Run from the repository root with backend requirements, httpx, FFmpeg, ffprobe, Node, and an existing Playwright/Chromium installation:

    python -m unittest discover -s tests -p 'test_*.py'
    node --test tests/test_frontend_auth.cjs tests/test_batch_state.cjs
    python tests/batch_fixture_server.py
    node tests/test_batch_browser.cjs  # second terminal

Small fixtures and browser artifacts go to $KLIPNIVO_TEST_DIR (default /tmp/klipnivo-tests). The browser suite uses Chromium's sandbox. The test-only downloader adapter never loads in production. It exercises 51 pasted links (50 supported fixture URLs and one invalid URL), real MP4 Files cached by sequential fetch, no automatic download navigation, one-tap Web Share, one automatic retry, manual retry, concurrency 1, attachments, ZIP selection, playback, MP3, mobile layout/device guides, and simulated Web Share handoff/cancellation. The subprocess integration test additionally holds a conversion at 82%, verifies the stall watchdog kills it and its encoder, and checks strict ordering through actual attachment completion. Set KLIPNIVO_STALL_TEST_SECONDS=75 for the full-duration watchdog test. Native iOS share sheets/Files permissions require a physical-device check.

Streaming tests (`test_streaming.py`) exercise ten ordered HTTP-backed files, direct MP4 passthrough, video-copy audio conversion/muxing, early output, one metadata prefetch, packet hashes, audio/video decode, ZIP integrity/ranges, temporary-file cleanup, MP3, timing logs, and failed-stream rollback/retries. The full suite runs with the standard Python discovery command above.

Save flow: component tests exercise 15 ordered fetched Files, zero automatic anchor clicks, synchronous one-tap multi-file/per-file sharing, selection, abort/stall recovery, retry loading, local Blob previews, and restoring files after refresh. ZIP remains a visible link activated only by the user.
