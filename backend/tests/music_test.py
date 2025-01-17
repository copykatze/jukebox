import json
import os
from threading import Thread

from django.conf import settings as conf
from django.urls import reverse

from core.musiq import playback, controller
from core.settings import storage
from tests import util
from tests.raveberry_test import RaveberryTest


class MusicTest(RaveberryTest):
    def setUp(self):
        super().setUp()

        # reduce number of downloaded songs for the test
        storage.put("max_playlist_items", 5)

        # for testing we set CELERY_ALWAY_EAGER, which runs all tasks in sync
        # the playback loop must still run in background of course
        # thus, we run it in a background thread instead of using playback.start()
        # which uses a celery task that would never end
        self.playback_thread = Thread(
            target=playback._loop  # pylint: disable=protected-access
        )
        # TODO: there was (is?) an issue where skipping a test did not call tearDown,
        # leaving this thread running.
        self.playback_thread.start()

        # mute player for testing
        self.player = controller.PLAYER
        self.player.mixer.set_volume(0)

    def tearDown(self):
        # restore player state
        storage.put("autoplay", False)
        self._poll_musiq_state(lambda state: not state["musiq"]["autoplay"])

        # ensure that the player is not waiting for a song to finish
        self.client.post(reverse("remove-all"))
        self._poll_musiq_state(lambda state: len(state["musiq"]["songQueue"]) == 0)
        self.client.post(reverse("skip"))
        self._poll_musiq_state(lambda state: not state["musiq"]["currentSong"])

        playback.stop()
        self.playback_thread.join(timeout=10)

        super().tearDown()

    def _setup_test_library(self):
        if not util.download_test_library():
            self.skipTest("could not download test library")

        test_library = os.path.join(conf.TEST_CACHE_DIR, "test_library")
        self.client.post(reverse("scan-library"), {"library_path": test_library})
        # need to split the scan_progress as it contains no-break spaces
        self._poll_state(
            "settings-state",
            lambda state: " ".join(
                state["settings"]["scanProgress"].split()
            ).startswith("5 / 5 / "),
        )
        self.client.post(reverse("create-playlists"))
        self._poll_state(
            "settings-state",
            lambda state: " ".join(
                state["settings"]["scanProgress"].split()
            ).startswith("5 / 5 / "),
        )

    def _poll_current_song(self):
        state = self._poll_musiq_state(
            lambda state: state["musiq"]["currentSong"], timeout=10
        )
        current_song = state["musiq"]["currentSong"]
        return current_song

    def _add_local_playlist(self):
        for term in "other", "heroes":
            suggestion = json.loads(
                self.client.get(
                    reverse("offline-suggestions"), {"term": term, "playlist": "true"}
                ).content
            )[0]
            self.client.post(
                reverse("request-music"),
                {
                    "key": suggestion["key"],
                    "query": "",
                    "playlist": "true",
                    "platform": "local",
                },
            )
        state = self._poll_musiq_state(
            lambda state: state["musiq"]["currentSong"]
            and len(state["musiq"]["songQueue"]) == 4
            and all(song["internalUrl"] for song in state["musiq"]["songQueue"]),
            timeout=3,
        )
        return state

    def _request_suggestion(self, key):
        self.client.post(
            reverse("request-music"),
            {"key": key, "query": "", "playlist": "false", "platform": "local"},
        )

    def _wait_for_new_song(self, old_id):
        self._poll_musiq_state(
            lambda state: len(state["musiq"]["songQueue"]) == 1
            and state["musiq"]["songQueue"][0]["internalUrl"]
            and state["musiq"]["songQueue"][0]["id"] != old_id,
            timeout=20,
        )
