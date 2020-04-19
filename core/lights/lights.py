"""This module handles the visualizations for leds and screen."""

import threading
import time
from functools import wraps

from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.http import HttpResponseForbidden
from django.shortcuts import render

import core.state_handler as state_handler
from core.lights.circle.circle import Circle
from core.lights.programs import Adaptive
from core.lights.programs import Alarm
from core.lights.programs import Cava
from core.lights.programs import Disabled
from core.lights.programs import Fixed
from core.lights.programs import Rainbow
from core.lights.ring import Ring
from core.lights.screen import Screen
from core.lights.strip import Strip
from core.models import Setting
from core.util import background_thread


def option(func):
    """A decorator that makes sure that all changes to the lights are synchronized."""

    def _decorator(self, request, *args, **kwargs):
        # only privileged users can change options during voting system
        if (
            self.base.settings.voting_system
            and not self.base.user_manager.has_controls(request.user)
        ):
            return HttpResponseForbidden()
        # don't allow option changes during alarm
        if self.base.musiq.player.alarm_playing.is_set():
            return HttpResponseForbidden()
        with self.option_lock:
            try:
                response = func(self, request, *args, **kwargs)
                if response is not None:
                    return response
            except (ValueError, IndexError) as e:
                print("error during lights option: " + str(e))
                return HttpResponseBadRequest()
            self.update_state()
        return HttpResponse()

    return wraps(func)(_decorator)


class Lights(state_handler.Stateful):
    """This class manages the updating of visualizations
    and provides endpoints to control them."""

    UPS = 30

    def __init__(self, base):
        self.seconds_per_frame = 1 / self.UPS

        self.base = base
        self.ring = Ring()
        self.strip = Strip()
        self.screen = Screen()

        # if the led loop is running
        self.loop_active = threading.Event()

        self.cava_program = Cava(self)
        self.alarm_program = Alarm(self)
        # a dictionary containing all programs by their name
        self.programs = {}
        for program_class in [Disabled, Fixed, Alarm, Rainbow, Adaptive, Circle]:
            instance = program_class(self)
            self.programs[instance.name] = instance

        # this lock ensures that only one thread changes led options
        self.option_lock = threading.Lock()
        self.program_speed = 1

        last_ring_program_name = self.base.settings.get_setting(
            "last_ring_program", "Disabled"
        )
        last_strip_program_name = self.base.settings.get_setting(
            "last_strip_program", "Disabled"
        )
        last_screen_program_name = self.base.settings.get_setting(
            "last_screen_program", "Disabled"
        )
        ring_program_name = self.base.settings.get_setting("ring_program", "Disabled")
        strip_program_name = self.base.settings.get_setting("strip_program", "Disabled")
        screen_program_name = self.base.settings.get_setting(
            "screen_program", "Disabled"
        )

        self.last_ring_program = self.programs[last_ring_program_name]
        self.last_strip_program = self.programs[last_strip_program_name]
        self.last_screen_program = self.programs[last_screen_program_name]
        self.last_fixed_color = self.programs["Fixed"].color

        self.ring_program = self.programs[ring_program_name]
        self.strip_program = self.programs[strip_program_name]
        self.screen_program = self.programs[screen_program_name]
        # disable disconnected devices
        if not self.ring.initialized:
            self.ring_program = self.programs["Disabled"]
        if not self.strip.initialized:
            self.strip_program = self.programs["Disabled"]
        if not self.screen.initialized:
            self.screen_program = self.programs["Disabled"]
        self.ring_program.use()
        self.strip_program.use()
        self.screen_program.use()
        self._consumers_changed()

        self._loop()

    @background_thread
    def _loop(self):
        iteration_count = 0
        adaptive_quality_window = self.UPS * 10
        time_sum = 0
        while True:
            self.loop_active.wait()

            with self.option_lock:
                computation_start = time.time()

                # these programs only actually do work if their respective programs are active
                self.cava_program.compute()
                self.alarm_program.compute()

                if self.screen_program.name != "Disabled":
                    self.screen_program.draw()

                self.ring_program.compute()
                if self.strip_program != self.ring_program:
                    self.strip_program.compute()

                if self.ring_program.name != "Disabled":
                    if self.ring.monochrome:
                        ring_colors = [
                            self.ring_program.strip_color()
                            for _ in range(self.ring.LED_COUNT)
                        ]
                    else:
                        ring_colors = self.ring_program.ring_colors()
                    self.ring.set_colors(ring_colors)

                if self.strip_program.name != "Disabled":
                    strip_color = self.strip_program.strip_color()
                    self.strip.set_color(strip_color)

            computation_time = time.time() - computation_start

            if self.screen_program.name != "Disabled":
                time_sum += computation_time
                iteration_count += 1
                if (
                    iteration_count >= adaptive_quality_window
                    or time_sum
                    >= 1.5 * adaptive_quality_window * self.seconds_per_frame
                ):
                    average_computation_time = time_sum / adaptive_quality_window
                    iteration_count = 0
                    time_sum = 0

                    # print(f"avg: {average_computation_time/self.seconds_per_frame}")
                    if average_computation_time > 0.9 * self.seconds_per_frame:
                        # if the loop takes too long and a screen program is active,
                        # it can be reduced in resolution to save time
                        self.screen_program.decrease_resolution()
                    elif average_computation_time < 0.6 * self.seconds_per_frame:
                        # if the loop has time to spare and a screen program is active,
                        # we can increase its quality
                        self.screen_program.increase_resolution()

            # print(f'computation took {computation_time:.5f}s')
            try:
                time.sleep(self.seconds_per_frame - computation_time)
            except ValueError:
                pass

    def _consumers_changed(self):
        if self.programs["Disabled"].consumers == 3:
            self.loop_active.clear()
        else:
            self.loop_active.set()

    def _set_ring_program(self, program, transient=False):
        # don't allow program change on disconnected devices
        if not self.ring.initialized:
            return

        self.ring_program.release()
        program.use()

        self.last_ring_program = self.ring_program
        self.ring_program = program
        if not transient:
            Setting.objects.filter(key="last_ring_program").update(
                value=self.last_ring_program.name
            )
            Setting.objects.filter(key="ring_program").update(
                value=self.ring_program.name
            )
        self._consumers_changed()

        if program.name == "Disabled":
            self.ring.clear()

    def _set_strip_program(self, program, transient=False):
        # don't allow program change on disconnected devices
        if not self.strip.initialized:
            return

        self.strip_program.release()
        program.use()

        self.last_strip_program = self.strip_program
        self.strip_program = program
        if not transient:
            Setting.objects.filter(key="last_strip_program").update(
                value=self.last_strip_program.name
            )
            Setting.objects.filter(key="strip_program").update(
                value=self.strip_program.name
            )
        self._consumers_changed()

        if program.name == "Disabled":
            self.strip.clear()

    def _set_screen_program(self, program, transient=False):
        if not self.screen.initialized:
            return
        self.screen_program.release()
        program.use()

        self.last_screen_program = self.screen_program
        self.screen_program = program
        if not transient:
            Setting.objects.filter(key="last_screen_program").update(
                value=self.last_screen_program.name
            )
            Setting.objects.filter(key="screen_program").update(
                value=self.screen_program.name
            )
        self._consumers_changed()

    def alarm_started(self):
        """Makes alarm the current program but doesn't update the database."""
        with self.option_lock:
            self.alarm_program.use()
            self.last_fixed_color = self.programs["Fixed"].color
            self._set_ring_program(self.programs["Fixed"], transient=True)
            self._set_strip_program(self.programs["Fixed"], transient=True)
            # the screen program adapts with the alarm and is not changed

    def alarm_stopped(self):
        """Restores the state from before the alarm."""
        with self.option_lock:
            self.alarm_program.release()
            self.programs["Fixed"].color = self.last_fixed_color
            self._set_ring_program(self.last_ring_program, transient=True)
            self._set_strip_program(self.last_strip_program, transient=True)

            # read last programs from database, which is still in the state before the alarm
            last_ring_program_name = Setting.objects.get(key="last_ring_program").value
            last_strip_program_name = Setting.objects.get(
                key="last_strip_program"
            ).value
            self.last_ring_program = self.programs[last_ring_program_name]
            self.last_strip_program = self.programs[last_strip_program_name]

    def state_dict(self):
        state_dict = self.base.state_dict()
        state_dict["ring_connected"] = self.ring.initialized
        state_dict["ring_program"] = self.ring_program.name
        state_dict["ring_brightness"] = self.ring.brightness
        state_dict["ring_monochrome"] = self.ring.monochrome
        state_dict["strip_connected"] = self.strip.initialized
        state_dict["strip_program"] = self.strip_program.name
        state_dict["strip_brightness"] = self.strip.brightness
        state_dict["screen_connected"] = self.screen.initialized
        state_dict["screen_program"] = self.screen_program.name
        state_dict["program_speed"] = self.program_speed
        state_dict["fixed_color"] = "#{:02x}{:02x}{:02x}".format(
            *(int(val * 255) for val in self.programs["Fixed"].color)
        )
        return state_dict

    def index(self, request):
        """Renders the /lights page."""
        context = self.base.context(request)
        # programs that have a strip_color or ring_color function are color programs
        # programs that have a draw function are screen programs
        context["color_program_names"] = [
            program.name
            for program in self.programs.values()
            if getattr(program, "ring_colors", None)
        ]
        context["screen_program_names"] = [
            program.name
            for program in self.programs.values()
            if getattr(program, "draw", None)
        ]
        # context['program_names'].remove('Alarm')
        return render(request, "lights.html", context)

    @option
    def set_lights_shortcut(self, request):
        """Stores the current lights state and restores the previous one."""
        should_enable = request.POST.get("value") == "true"
        is_enabled = (
            self.ring_program.name != "Disabled"
            or self.strip_program.name != "Disabled"
        )
        if should_enable == is_enabled:
            return
        if should_enable:
            self._set_ring_program(self.last_ring_program)
            self._set_strip_program(self.last_strip_program)
        else:
            self._set_ring_program(self.programs["Disabled"])
            self._set_strip_program(self.programs["Disabled"])

    @option
    def set_ring_program(self, request):
        """Updates the ring program."""
        program_name = request.POST.get("program")
        program = self.programs[program_name]
        if program == self.ring_program:
            # the program doesn't change, return immediately
            return
        self._set_ring_program(program)

    @option
    def set_ring_brightness(self, request):
        """Updates the ring brightness."""
        # raises ValueError on wrong input, caught in option decorator
        value = float(request.POST.get("value"))
        self.ring.brightness = value

    @option
    def set_ring_monochrome(self, request):
        """Sets whether the ring should be in one color only."""
        enabled = request.POST.get("value") == "true"
        self.ring.monochrome = enabled

    @option
    def set_strip_program(self, request):
        """Updates the strip program."""
        program_name = request.POST.get("program")
        program = self.programs[program_name]
        if program == self.strip_program:
            # the program doesn't change, return immediately
            return
        self._set_strip_program(program)

    @option
    def set_strip_brightness(self, request):
        """Updates the strip brightness."""
        # raises ValueError on wrong input, caught in option decorator
        value = float(request.POST.get("value"))
        self.strip.brightness = value

    @option
    def adjust_screen(self, _request):
        """Adjusts the resolution of the screen."""
        if self.screen_program.name != "Disabled":
            return HttpResponseBadRequest(
                "Disable the screen program before readjusting"
            )
        self.screen.adjust()
        return HttpResponse()

    @option
    def set_screen_program(self, request):
        """Updates the screen program."""
        program_name = request.POST.get("program")
        program = self.programs[program_name]
        if program == self.screen_program:
            # the program doesn't change, return immediately
            return
        self._set_screen_program(program)

    @option
    def set_program_speed(self, request):
        """Updates the global speed of programs supporting it."""
        value = float(request.POST.get("value"))
        self.program_speed = value

    @option
    def set_fixed_color(self, request):
        """Updates the static color used for some programs."""
        hex_col = request.POST.get("value").lstrip("#")
        # raises IndexError on wrong input, caught in option decorator
        color = tuple(int(hex_col[i : i + 2], 16) / 255 for i in (0, 2, 4))
        self.programs["Fixed"].color = color
