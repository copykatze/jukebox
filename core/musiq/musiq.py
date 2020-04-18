"""This module handles all requests concerning the addition of music to the queue."""

import logging

import ipware
from django.forms.models import model_to_dict
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

import core.musiq.song_utils as song_utils
from core.models import CurrentSong
from core.models import QueuedSong
from core.musiq.localdrive import LocalSongProvider
from core.musiq.music_provider import SongProvider, PlaylistProvider
from core.musiq.player import Player
from core.musiq.spotify import SpotifySongProvider, SpotifyPlaylistProvider
from core.musiq.suggestions import Suggestions
from core.musiq.youtube import (
    YoutubeSongProvider,
    YoutubePlaylistProvider,
)
from core.state_handler import Stateful


class Musiq(Stateful):
    """This class provides endpoints for all music related requests."""

    def __init__(self, base):
        self.base = base

        self.logger = logging.getLogger("raveberry")

        self.suggestions = Suggestions(self)

        self.queue = QueuedSong.objects
        self.placeholders = []

        self.player = Player(self)
        self.player.start()

    def do_request_music(
        self,
        request_ip,
        query,
        key,
        playlist,
        platform,
        archive=True,
        manually_requested=True,
    ):
        """Performs the actual requesting of the music, not an endpoint.
        Enqueues the requested song or playlist into the queue, using appropriate providers."""
        providers = []

        if playlist:
            if key is not None:
                # an archived song was requested.
                # The key determines the SongProvider (Youtube or Spotify)
                provider = PlaylistProvider.create(self, query, key)
                if provider is None:
                    return HttpResponseBadRequest(
                        "No provider found for requested playlist"
                    )
                providers.append(provider)
            else:
                # try to use spotify if the user did not specifically request youtube
                if platform is None or platform == "spotify":
                    if self.base.settings.spotify_enabled:
                        providers.append(SpotifyPlaylistProvider(self, query, key))
                # use Youtube as a fallback
                providers.append(YoutubePlaylistProvider(self, query, key))
        else:
            if key is not None:
                # an archived song was requested.
                # The key determines the SongProvider (Youtube or Spotify)
                provider = SongProvider.create(self, query, key)
                if provider is None:
                    return HttpResponseBadRequest(
                        "No provider found for requested song"
                    )
                providers.append(provider)
            else:
                if platform == "local":
                    # if a local provider was requested,
                    # use only this one as its only possible source is the database
                    providers.append(LocalSongProvider(self, query, key))
                else:
                    # try to use spotify if the user did not specifically request youtube
                    if platform is None or platform == "spotify":
                        if self.base.settings.spotify_enabled:
                            try:
                                providers.append(SpotifySongProvider(self, query, key))
                            except ValueError:
                                pass
                    # use Youtube as a fallback
                    providers.append(YoutubeSongProvider(self, query, key))

        fallback = False
        used_provider = None
        for i, provider in enumerate(providers):
            if not provider.check_cached():
                if not provider.check_downloadable():
                    # this provider cannot provide this song, use the next provider
                    # if this was the last provider, show its error
                    if i == len(providers) - 1:
                        return HttpResponseBadRequest(provider.error)
                    fallback = True
                    continue
                if not provider.download(
                    request_ip, archive=archive, manually_requested=manually_requested
                ):
                    return HttpResponseBadRequest(provider.error)
            else:
                provider.enqueue(
                    request_ip, archive=archive, manually_requested=manually_requested
                )
            # the current provider could provide the song, don't try the other ones
            used_provider = provider
            break
        message = used_provider.ok_message
        if fallback:
            message += " (used fallback)"
        return HttpResponse(message)

    def request_music(self, request):
        """Endpoint to request music. Calls internal function."""
        key = request.POST.get("key")
        playlist = request.POST.get("playlist") == "true"
        query = request.POST.get("query")
        platform = request.POST.get("platform")

        # only get ip on user requests
        if self.base.settings.logging_enabled:
            request_ip, _ = ipware.get_client_ip(request)
            if request_ip is None:
                request_ip = ""
        else:
            request_ip = ""

        return self.do_request_music(request_ip, query, key, playlist, platform)

    def request_radio(self, request):
        """Endpoint to request radio for the current song."""
        # only get ip on user requests
        if self.base.settings.logging_enabled:
            request_ip, _ = ipware.get_client_ip(request)
            if request_ip is None:
                request_ip = ""
        else:
            request_ip = ""

        try:
            current_song = CurrentSong.objects.get()
        except CurrentSong.DoesNotExist:
            return HttpResponseBadRequest("Need a song to play the radio")
        provider = SongProvider.create(self, external_url=current_song.external_url)
        return provider.request_radio(request_ip)

    @csrf_exempt
    def post_song(self, request):
        """This endpoint is part of the API and exempt from CSRF checks.
        Shareberry uses this endpoint."""
        return self.request_music(request)

    def index(self, request):
        """Renders the /musiq page."""
        context = self.base.context(request)
        return render(request, "musiq.html", context)

    def state_dict(self):
        state_dict = self.base.state_dict()
        try:
            current_song = CurrentSong.objects.get()
            current_song = model_to_dict(current_song)
        except CurrentSong.DoesNotExist:
            current_song = None
        song_queue = []
        all_songs = self.queue.all()
        if self.base.settings.voting_system:
            all_songs = all_songs.order_by("-votes", "index")
        for song in all_songs:
            song_dict = model_to_dict(song)
            song_dict["duration_formatted"] = song_utils.format_seconds(
                song_dict["duration"]
            )
            song_dict["confirmed"] = True
            # find the query of the placeholder that this song replaces (if any)
            for placeholder in self.placeholders[:]:
                if placeholder["replaced_by"] == song.id:
                    song_dict["replaces"] = placeholder["query"]
                    self.placeholders.remove(placeholder)
                    break
            else:
                song_dict["replaces"] = None
            song_queue.append(song_dict)
        song_queue += [
            {"title": placeholder["query"], "confirmed": False}
            for placeholder in self.placeholders
        ]

        if state_dict["alarm"]:
            state_dict["current_song"] = {
                "queue_key": -1,
                "manually_requested": False,
                "votes": None,
                "internal_url": "",
                "external_url": "",
                "artist": "Raveberry",
                "title": "ALARM!",
                "duration": 10,
                "created": "",
            }
        else:
            state_dict["current_song"] = current_song
        state_dict["paused"] = self.player.paused()
        state_dict["progress"] = self.player.progress()
        state_dict["shuffle"] = self.player.shuffle
        state_dict["repeat"] = self.player.repeat
        state_dict["autoplay"] = self.player.autoplay
        state_dict["volume"] = self.player.volume
        state_dict["song_queue"] = song_queue
        return state_dict
