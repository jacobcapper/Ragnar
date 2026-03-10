# pisugar_button.py
"""
GPIO button listener for Ragnar (replaces PiSugar 3 button module).

Monitors a plain momentary push-button wired between a GPIO pin and GND and
triggers mode swaps between Ragnar and Pwnagotchi:
  - Single tap:  Toggle Ragnar manual mode on/off
  - Double tap:  Switch to Pwnagotchi (or back to Ragnar)
  - Long press:  Switch to Pwnagotchi (alternative trigger for reliability)

Default pin: BCM 23. Override with the GPIO_BUTTON_PIN environment variable.

If GPIO initialisation fails (gpiozero not installed, insufficient permissions,
or pin already in use) the listener logs a warning and disables itself silently
so the rest of Ragnar is unaffected.

The class is intentionally named PiSugarButtonListener to preserve the existing
import/instantiation in Ragnar.py without any changes to that file.
"""

import os
import threading
import logging
import time

try:
    from logger import Logger
    logger = Logger(name="pisugar_button.py", level=logging.DEBUG)
except Exception:
    import logging as _logging
    logger = _logging.getLogger("pisugar_button")

# ── Tunable constants ──────────────────────────────────────────────────────────
_DEFAULT_PIN = 23       # BCM pin number; wired to GND via a momentary button
_TAP_WINDOW = 0.4       # seconds: window to detect a second tap for double-tap
_LONG_PRESS_TIME = 1.0  # seconds: hold duration that triggers a long press
_BOUNCE_TIME = 0.05     # seconds: debounce period (50 ms)
_SWAP_COOLDOWN = 10     # seconds: minimum gap between swap triggers
# ──────────────────────────────────────────────────────────────────────────────


class PiSugarButtonListener:
    """GPIO button listener that preserves the PiSugar button interface for Ragnar."""

    def __init__(self, shared_data):
        self.shared_data = shared_data
        self._stop_event = threading.Event()
        self._swap_cooldown_ts = 0.0   # timestamp of last swap trigger
        self.available = False
        self._button = None

        # Resolve pin number from environment (falls back to _DEFAULT_PIN)
        pin_env = os.environ.get('GPIO_BUTTON_PIN', '').strip()
        try:
            self._pin = int(pin_env) if pin_env else _DEFAULT_PIN
        except ValueError:
            logger.warning(
                f"Invalid GPIO_BUTTON_PIN value '{pin_env}', using default BCM {_DEFAULT_PIN}"
            )
            self._pin = _DEFAULT_PIN

        # Tap-detection state
        self._lock = threading.Lock()
        self._last_release_ts = 0.0        # time of the most recent release
        self._pending_single_timer = None  # Timer that fires a deferred single tap
        self._held_fired = False           # True while a long-press has been dispatched

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        """Set up the GPIO button.  Silently disables itself on any failure."""
        try:
            from gpiozero import Button
        except ImportError:
            logger.warning("gpiozero not installed — GPIO button listener disabled")
            return

        try:
            self._button = Button(
                self._pin,
                pull_up=True,              # internal pull-up; button connects pin to GND
                hold_time=_LONG_PRESS_TIME,
                bounce_time=_BOUNCE_TIME,
            )
            self._button.when_released = self._on_released
            self._button.when_held = self._on_held
            self._button_active = True
            # self.available intentionally stays False — it signals *battery* hardware
            # presence to display.py / webapp_modern.py.  There is no battery here.
            logger.info(f"GPIO button listener active on BCM pin {self._pin}")
        except Exception as e:
            logger.warning(
                f"GPIO button init failed (BCM pin {self._pin}): {e} — listener disabled"
            )

    def stop(self):
        """Stop the listener and release GPIO resources."""
        self._stop_event.set()
        with self._lock:
            if self._pending_single_timer is not None:
                self._pending_single_timer.cancel()
                self._pending_single_timer = None
        if self._button is not None:
            try:
                self._button.close()
            except Exception:
                pass

    # ── GPIO callbacks (called from gpiozero's internal thread) ───────────────

    def _on_held(self):
        """Fires after the button has been held for _LONG_PRESS_TIME seconds."""
        # Cancel any pending single-tap timer — long press takes priority
        with self._lock:
            self._held_fired = True
            if self._pending_single_timer is not None:
                self._pending_single_timer.cancel()
                self._pending_single_timer = None
        self._on_long_tap()

    def _on_released(self):
        """Fires on every button release; classifies as single- or double-tap."""
        # If a long press was already handled, consume the release and reset the flag
        with self._lock:
            if self._held_fired:
                self._held_fired = False
                return

        now = time.time()

        with self._lock:
            elapsed_since_last = now - self._last_release_ts
            self._last_release_ts = now

            if elapsed_since_last < _TAP_WINDOW:
                # Second tap within the window → double tap
                if self._pending_single_timer is not None:
                    self._pending_single_timer.cancel()
                    self._pending_single_timer = None
                # Run handler off this callback thread to avoid blocking gpiozero
                threading.Thread(target=self._on_double_tap, daemon=True).start()
            else:
                # Could be the first tap of a double tap; defer the single-tap action
                if self._pending_single_timer is not None:
                    self._pending_single_timer.cancel()
                t = threading.Timer(_TAP_WINDOW, self._fire_single_tap)
                t.daemon = True
                self._pending_single_timer = t
                t.start()

    def _fire_single_tap(self):
        """Called by the deferred timer when no second tap arrived."""
        with self._lock:
            self._pending_single_timer = None
        self._on_single_tap()

    # ── Action handlers ────────────────────────────────────────────────────────

    def _on_single_tap(self):
        """Single tap: toggle Ragnar manual mode."""
        try:
            current = self.shared_data.config.get('manual_mode', False)
            new_mode = not current
            self.shared_data.config['manual_mode'] = new_mode

            ragnar = getattr(self.shared_data, 'ragnar_instance', None)
            if ragnar:
                if new_mode:
                    ragnar.stop_orchestrator()
                    logger.info("GPIO tap: manual mode ON (orchestrator stopped)")
                else:
                    ragnar.start_orchestrator()
                    logger.info("GPIO tap: manual mode OFF (orchestrator started)")
        except Exception as e:
            logger.error(f"GPIO single tap handler error: {e}")

    def _on_double_tap(self):
        """Double tap: swap between Ragnar and Pwnagotchi."""
        self._trigger_swap()

    def _on_long_tap(self):
        """Long press: swap between Ragnar and Pwnagotchi (alternative trigger)."""
        self._trigger_swap()

    def _trigger_swap(self):
        """Trigger a mode swap with a cooldown to prevent accidental double triggers."""
        now = time.time()
        if now - self._swap_cooldown_ts < _SWAP_COOLDOWN:
            logger.debug("GPIO button swap ignored — cooldown active")
            return
        self._swap_cooldown_ts = now

        try:
            current_mode = self.shared_data.config.get('pwnagotchi_mode', 'ragnar')
            target = 'pwnagotchi' if current_mode != 'pwnagotchi' else 'ragnar'

            logger.info(f"GPIO button: swapping to {target}")

            from webapp_modern import (
                _schedule_pwn_mode_switch,
                _write_pwn_status_file,
                _update_pwn_config,
                _emit_pwn_status_update,
            )
            _write_pwn_status_file(
                'switching',
                f'Button-triggered swap to {target}',
                'swap',
                {'target_mode': target},
            )
            _update_pwn_config({
                'pwnagotchi_mode': target,
                'pwnagotchi_last_status': f'Swapping to {target} (button)',
            })
            _emit_pwn_status_update()
            _schedule_pwn_mode_switch(target)

        except Exception as e:
            logger.error(f"GPIO button swap trigger failed: {e}")

    # ── Public getters (battery API stubs — no battery hardware present) ───────
    #
    # webapp_modern.py and display.py both call these methods on the listener
    # object.  Since there is no PiSugar battery, all battery values are None.
    # The callers already guard against None returns.

    def get_battery_level(self):
        """No battery hardware present; always returns None."""
        return None

    def is_charging(self):
        """No battery hardware present; always returns None."""
        return None

    def get_battery_voltage(self):
        """No battery hardware present; always returns None."""
        return None

    def get_model(self):
        """Return a short description of the hardware in use."""
        if not getattr(self, '_button_active', False):
            return None
        return f"GPIO Button (BCM {self._pin})"
