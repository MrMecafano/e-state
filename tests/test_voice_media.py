"""Offline voice/media security regressions; all credentials and process runners are synthetic."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from estate.connectors import Attachment
from estate.instances import InstanceRegistry
from estate.kb import Listing
from estate.media import MediaError, MediaGateway
from estate.voice import (MAX_AUDIO_BYTES, VoiceError, WhisperCppTranscriber,
                          instagram_audio, telegram_file)

TG_TOKEN = "900001:synthetic_voice_media_only"
OTHER_TOKEN = "900002:synthetic_other_tenant_only"
WAVE = b"RIFF\x00\x00\x00\x00WAVEsynthetic_pcm"


class OfflineVoiceFixture(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket", side_effect=AssertionError("live network forbidden")).start()
        patch.dict(os.environ, {}, clear=True).start()


class WhisperProcessIntegrationTest(OfflineVoiceFixture):
    def runner(self, arguments, **kwargs):
        self.process_calls.append((arguments, kwargs))
        if "-of" in arguments:
            Path(arguments[arguments.index("-of") + 1] + ".txt").write_text(
                "  Юнусободда икки хонали уй керак  \n", encoding="utf-8")
        else:
            Path(arguments[-1]).write_bytes(WAVE)
        return SimpleNamespace(returncode=0)

    def setUp(self):
        super().setUp()
        self.process_calls = []

    def test_injected_runner_writes_of_transcript_and_receives_bounded_clean_environment(self):
        transcriber = WhisperCppTranscriber("synthetic-whisper", "synthetic-model.bin", runner=self.runner)
        text = transcriber.transcribe(WAVE)
        self.assertEqual(text, "Юнусободда икки хонали уй керак")
        arguments, kwargs = self.process_calls[0]
        self.assertEqual(arguments[:3], ["synthetic-whisper", "-m", "synthetic-model.bin"])
        self.assertEqual(arguments[arguments.index("-l") + 1], "auto")
        self.assertIn("-otxt", arguments)
        self.assertEqual(kwargs["timeout"], 25)
        self.assertFalse(kwargs["check"])
        self.assertEqual(set(kwargs["env"]), {"PATH", "LANG"})
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(Path(arguments[arguments.index("-of") + 1]).parent.exists())

    def test_ffmpeg_and_supported_languages_use_injected_runner_only(self):
        for language in ("uz", "ru"):
            with self.subTest(language=language):
                self.process_calls.clear()
                transcriber = WhisperCppTranscriber("synthetic-whisper", "synthetic-model", "synthetic-ffmpeg",
                                                     runner=self.runner, timeout=7)
                self.assertTrue(transcriber.transcribe(b"synthetic_ogg", language=language))
                self.assertEqual(len(self.process_calls), 2)
                self.assertEqual(self.process_calls[0][0][0], "synthetic-ffmpeg")
                self.assertIn("16000", self.process_calls[0][0])
                self.assertEqual(self.process_calls[1][0][self.process_calls[1][0].index("-l") + 1], language)
                self.assertTrue(all(call[1]["timeout"] == 7 for call in self.process_calls))

    def test_timeout_and_nonzero_process_exit_are_sanitized(self):
        for failure in (subprocess.TimeoutExpired("synthetic_secret_command", 1),
                        SimpleNamespace(returncode=1)):
            runner = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
            with self.subTest(failure=type(failure).__name__), self.assertRaises(VoiceError) as caught:
                WhisperCppTranscriber("synthetic-whisper", "synthetic-model", runner=runner).transcribe(WAVE)
            self.assertEqual(str(caught.exception), "Local transcription failed")
            self.assertNotIn("synthetic_secret_command", str(caught.exception))

    def test_missing_empty_and_oversized_transcripts_are_rejected(self):
        for value in (None, "  ", "x" * (32 * 1024 + 1)):
            def runner(arguments, **kwargs):
                if value is not None:
                    Path(arguments[arguments.index("-of") + 1] + ".txt").write_text(value)
                return SimpleNamespace(returncode=0)
            with self.subTest(value_size=None if value is None else len(value)), self.assertRaises(VoiceError):
                WhisperCppTranscriber("synthetic-whisper", "synthetic-model", runner=runner).transcribe(WAVE)

    def test_invalid_audio_language_and_missing_ffmpeg_do_not_launch_process(self):
        runner = Mock()
        transcriber = WhisperCppTranscriber("synthetic-whisper", "synthetic-model", runner=runner)
        for audio in (b"", "not bytes", b"x" * (MAX_AUDIO_BYTES + 1), b"synthetic_ogg"):
            with self.subTest(audio_type=type(audio).__name__), self.assertRaises(VoiceError):
                transcriber.transcribe(audio)
        with self.assertRaises(VoiceError):
            transcriber.transcribe(WAVE, language="en")
        runner.assert_not_called()

    def test_environment_configuration_is_optional_and_never_launches_process(self):
        self.assertIsNone(WhisperCppTranscriber.from_environment())
        with patch.dict(os.environ, {"WHISPER_CPP_BIN": "synthetic-whisper"}, clear=True):
            with self.assertRaises(VoiceError):
                WhisperCppTranscriber.from_environment()
        with patch.dict(os.environ, {"WHISPER_CPP_BIN": "synthetic-whisper", "WHISPER_MODEL_PATH": "synthetic-model"}, clear=True):
            self.assertIsInstance(WhisperCppTranscriber.from_environment(), WhisperCppTranscriber)


class AudioDownloadIntegrationTest(OfflineVoiceFixture):
    def test_telegram_getfile_and_download_remain_on_official_fixed_origin(self):
        calls = []
        def transport(request, *, timeout):
            calls.append((request.full_url, request.get_method(), request.data, timeout))
            if len(calls) == 1:
                return 200, json.dumps({"ok": True, "result": {"file_path": "voice/synthetic.ogg", "file_size": 5}}).encode()
            return 200, b"audio"
        self.assertEqual(telegram_file(TG_TOKEN, "synthetic_file_id", transport=transport),
                         (b"audio", "voice/synthetic.ogg"))
        self.assertEqual(calls[0][0], "https://api.telegram.org/bot" + TG_TOKEN + "/getFile")
        self.assertEqual(calls[0][1], "POST")
        self.assertEqual(json.loads(calls[0][2]), {"file_id": "synthetic_file_id"})
        self.assertEqual(calls[1][0], "https://api.telegram.org/file/bot" + TG_TOKEN + "/voice/synthetic.ogg")
        self.assertTrue(all(urlsplit(call[0]).hostname == "api.telegram.org" for call in calls))
        self.assertTrue(all(call[3] == 10 for call in calls))

    def test_telegram_rejects_external_or_traversal_file_paths_before_download(self):
        for path in ("../private", "voice/../private", "/voice/a.ogg", "https://evil.example.com/a",
                     "voice//a.ogg", "voice/%2e%2e/a.ogg"):
            transport = Mock(return_value=(200, json.dumps({"ok": True, "result": {"file_path": path}}).encode()))
            with self.subTest(path=path), self.assertRaises(VoiceError) as caught:
                telegram_file(TG_TOKEN, "synthetic_file", transport=transport)
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(str(caught.exception), "Telegram file download failed")
            self.assertNotIn(TG_TOKEN, str(caught.exception))

    def test_telegram_invalid_ids_and_credentials_do_not_issue_requests(self):
        transport = Mock()
        for token, file_id in (("", "synthetic"), (TG_TOKEN, "https://evil.example.com/a"),
                               (TG_TOKEN, "../private"), (TG_TOKEN, "")):
            with self.subTest(file_id=file_id), self.assertRaises(VoiceError):
                telegram_file(token, file_id, transport=transport)
        transport.assert_not_called()

    def test_telegram_metadata_limits_actual_limits_and_provider_errors_are_sanitized(self):
        for result in ({"file_path": "voice/a.ogg", "file_size": 6},
                       {"file_path": "voice/a.ogg", "file_size": True}, {"file_path": "voice/a.ogg", "file_size": -1}):
            transport = Mock(return_value=(200, json.dumps({"ok": True, "result": result}).encode()))
            with self.subTest(result=result), self.assertRaises(VoiceError):
                telegram_file(TG_TOKEN, "synthetic", transport=transport, max_bytes=5)
            self.assertEqual(transport.call_count, 1)
        transport = Mock(side_effect=[(200, b'{"ok":true,"result":{"file_path":"voice/a.ogg"}}'), (200, b"123456")])
        with self.assertRaises(VoiceError):
            telegram_file(TG_TOKEN, "synthetic", transport=transport, max_bytes=5)
        transport = Mock(side_effect=OSError("synthetic private URL " + TG_TOKEN))
        with self.assertRaises(VoiceError) as caught:
            telegram_file(TG_TOKEN, "synthetic", transport=transport)
        self.assertNotIn(TG_TOKEN, str(caught.exception))
        self.assertNotIn("private URL", str(caught.exception))

    def test_meta_owned_audio_origins_are_allowed_without_live_fetch(self):
        transport = Mock(return_value=(200, b"synthetic_audio"))
        for host in ("scontent.cdninstagram.com", "scontent.fbcdn.net", "lookaside.fbsbx.com"):
            with self.subTest(host=host):
                url = "https://" + host + "/synthetic.ogg?signature=synthetic_only"
                self.assertEqual(instagram_audio(url, transport=transport), b"synthetic_audio")
                self.assertEqual(transport.call_args.args[0].full_url, url)
                self.assertEqual(transport.call_args.kwargs["timeout"], 10)

    def test_meta_untrusted_origins_ports_credentials_and_redirects_are_rejected(self):
        transport = Mock(return_value=(200, b"audio"))
        for url in ("http://scontent.fbcdn.net/a", "https://fbcdn.net.evil.example.com/a",
                    "https://evilfbcdn.net/a", "https://127.0.0.1/a", "file:///private",
                    "https://user:password@scontent.fbcdn.net/a", "https://scontent.fbcdn.net:8443/a"):
            with self.subTest(url=url), self.assertRaises(VoiceError):
                instagram_audio(url, transport=transport)
        transport.assert_not_called()
        transport.return_value = (302, b"synthetic_redirect_private_body")
        with self.assertRaises(VoiceError) as caught:
            instagram_audio("https://scontent.fbcdn.net/a", transport=transport)
        self.assertEqual(str(caught.exception), "Instagram audio download failed")


class SignedMediaIntegrationTest(OfflineVoiceFixture):
    def setUp(self):
        super().setUp()
        patch.dict(os.environ, {"SYNTHETIC_MEDIA_TG_A": TG_TOKEN, "SYNTHETIC_MEDIA_TG_B": OTHER_TOKEN}, clear=True).start()
        self.registry = InstanceRegistry()
        for tenant in ("a", "b"):
            instance = self.registry.create(tenant, "Synthetic " + tenant, telegram_channel="@synthetic_" + tenant,
                                           telegram_token_env="SYNTHETIC_MEDIA_TG_" + tenant.upper())
            instance.kb.add(Listing("L-001", "Synthetic", "Yunusobod", 2, 50, 500,
                                    photos=["synthetic_photo_" + tenant], videos=["synthetic_video_" + tenant]))
        self.now = 1000
        self.fetcher = Mock(return_value=(b"synthetic_image", "photos/synthetic.jpg"))
        self.gateway = MediaGateway(self.registry, "https://media.example.com", "synthetic_signing_key_only_123456",
                                    clock=lambda: self.now, fetcher=self.fetcher)

    def capability(self, tenant="a", kind="photo"):
        instance = self.registry.get(tenant)
        listing = instance.kb.get("L-001")
        reference = (listing.photos if kind == "photo" else listing.videos)[0]
        attachment = self.gateway.attachment(instance, listing, kind, 0, reference)
        parts = urlsplit(attachment.url)
        return attachment, parts.path, parse_qs(parts.query)

    def test_generated_url_is_tenant_bound_signed_and_contains_no_bot_token_or_file_reference(self):
        attachment, path, query = self.capability()
        self.assertEqual(attachment.kind, "photo")
        self.assertEqual(urlsplit(attachment.url).netloc, "media.example.com")
        self.assertEqual(path, "/media/a/L-001/photo/0")
        self.assertEqual(query["expires"], ["1600"])
        for private in (TG_TOKEN, OTHER_TOKEN, "synthetic_photo_a", "/file/bot"):
            self.assertNotIn(private, attachment.url)
        self.assertEqual(self.gateway.fetch(path, query), (b"synthetic_image", "image/jpeg"))
        self.fetcher.assert_called_once_with(TG_TOKEN, "synthetic_photo_a", max_bytes=20 * 1024 * 1024)

    def test_tampering_tenant_listing_kind_index_expiry_or_signature_prevents_fetch(self):
        _, path, query = self.capability()
        cases = [(path.replace("/a/", "/b/"), query), (path.replace("L-001", "L-002"), query),
                 (path.replace("/photo/", "/video/"), query), (path[:-1] + "1", query),
                 (path, {**query, "expires": ["1599"]}), (path, {**query, "signature": ["0" * 64]})]
        for target, supplied in cases:
            with self.subTest(path=target, query=supplied), self.assertRaises(MediaError):
                self.gateway.fetch(target, supplied)
        self.fetcher.assert_not_called()

    def test_expiry_boundary_future_and_ambiguous_queries_prevent_fetch(self):
        _, path, query = self.capability()
        self.now = 1600
        with self.assertRaises(MediaError):
            self.gateway.fetch(path, query)
        self.now = 999
        with self.assertRaises(MediaError):
            self.gateway.fetch(path, query)
        self.now = 1000
        for supplied in ({**query, "extra": ["x"]}, {**query, "signature": query["signature"] * 2},
                         {**query, "expires": []}, {"expires": query["expires"]}):
            with self.subTest(query=supplied), self.assertRaises(MediaError):
                self.gateway.fetch(path, supplied)
        self.fetcher.assert_not_called()

    def test_taken_listing_cannot_be_served_by_still_valid_capability(self):
        _, path, query = self.capability()
        self.registry.get("a").kb.apply_control_message("L-001 taken")
        with self.assertRaises(MediaError):
            self.gateway.fetch(path, query)
        self.fetcher.assert_not_called()

    def test_other_tenant_capability_resolves_only_other_tenant_credentials_and_file(self):
        _, path, query = self.capability("b")
        self.gateway.fetch(path, query)
        self.fetcher.assert_called_once_with(OTHER_TOKEN, "synthetic_photo_b", max_bytes=20 * 1024 * 1024)

    def test_video_requires_video_mime_and_photo_rejects_mismatched_content(self):
        _, path, query = self.capability(kind="video")
        self.fetcher.return_value = (b"synthetic_video", "videos/synthetic.mp4")
        self.assertEqual(self.gateway.fetch(path, query), (b"synthetic_video", "video/mp4"))
        _, path, query = self.capability()
        with self.assertRaises(MediaError):
            self.gateway.fetch(path, query)

    def test_provider_failure_is_sanitized(self):
        _, path, query = self.capability()
        self.fetcher.side_effect = OSError("synthetic private source " + TG_TOKEN)
        with self.assertRaises(MediaError) as caught:
            self.gateway.fetch(path, query)
        self.assertNotIn(TG_TOKEN, str(caught.exception))
        self.assertNotIn("synthetic private source", str(caught.exception))

    def test_public_https_references_remain_public_without_including_credentials(self):
        instance = self.registry.get("a")
        attachment = self.gateway.attachment(instance, instance.kb.get("L-001"), "photo", 0,
                                             "https://cdn.example.com/synthetic.jpg")
        self.assertEqual(attachment, Attachment("photo", "https://cdn.example.com/synthetic.jpg"))
        self.fetcher.assert_not_called()

    def test_direct_telegram_token_urls_must_not_be_forwarded_as_public_media(self):
        instance = self.registry.get("a")
        private_url = "https://api.telegram.org/file/bot" + TG_TOKEN + "/photos/synthetic.jpg"
        with self.assertRaises(MediaError):
            self.gateway.attachment(instance, instance.kb.get("L-001"), "photo", 0, private_url)


if __name__ == "__main__":
    unittest.main()
