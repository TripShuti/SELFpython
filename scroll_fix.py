#!/usr/bin/env python3
import sys, time
from evdev import InputDevice, UInput, ecodes, InputEvent

PATH = sys.argv[1] if len(sys.argv) > 1 else "/dev/input/by-id/usb-E-Signal_Hator_Pulsar-event-mouse"

REQUIRED = 3    # скільки підряд тіків у новому напрямку треба, щоб повірити в розворот
IDLE_RESET = 1.0  # якщо пауза між скролами більша — розворот приймаємо одразу

REL_V = {ecodes.REL_WHEEL, ecodes.REL_WHEEL_HI_RES}
REL_H = {ecodes.REL_HWHEEL, ecodes.REL_HWHEEL_HI_RES}

def pkt_dir(pkt, codes):
    s = sum(e.value for e in pkt if e.type == ecodes.EV_REL and e.code in codes)
    return (s > 0) - (s < 0)

def clamp(ev):
    if ev.type == ecodes.EV_REL:
        if ev.code in (ecodes.REL_WHEEL_HI_RES, ecodes.REL_HWHEEL_HI_RES) and abs(ev.value) > 120:
            return InputEvent(ev.sec, ev.usec, ev.type, ev.code, 120 if ev.value > 0 else -120)
        if ev.code in (ecodes.REL_WHEEL, ecodes.REL_HWHEEL) and abs(ev.value) > 1:
            return InputEvent(ev.sec, ev.usec, ev.type, ev.code, 1 if ev.value > 0 else -1)
    return ev

class Axis:
    __slots__ = ("confirmed", "last_emit", "pending_dir", "pending_cnt", "pending_buf")
    def __init__(self):
        self.confirmed = 0
        self.last_emit = 0.0
        self.pending_dir = 0
        self.pending_cnt = 0
        self.pending_buf = []

    def clear_pending(self):
        self.pending_dir = 0
        self.pending_cnt = 0
        self.pending_buf = []

    # Повертає: "emit" | "hold" | ("flush", [events])
    def decide(self, d, now, held):
        if d == 0:
            return "emit"
        if self.confirmed == 0 or (now - self.last_emit) > IDLE_RESET:
            self.clear_pending()
            self.confirmed = d
            self.last_emit = now
            return "emit"
        if d == self.confirmed:
            if self.pending_dir == 0:
                self.last_emit = now
                return "emit"
            self.clear_pending()  # підозра не підтвердилась — це був дребезг
            self.last_emit = now
            return "emit"
        # d проти confirmed, вікно активне — вимагаємо підтвердження серією
        if self.pending_dir != d:
            self.pending_dir = d
            self.pending_cnt = 1
            self.pending_buf = list(held)
            return "hold"
        self.pending_cnt += 1
        self.pending_buf.extend(held)
        if self.pending_cnt >= REQUIRED:
            flushed = self.pending_buf
            self.clear_pending()
            self.confirmed = d
            self.last_emit = now
            return ("flush", flushed)
        return "hold"

def run_once():
    v = Axis()
    h = Axis()
    buf = []
    dev = InputDevice(PATH)
    try:
        ui = UInput.from_device(dev, name=dev.name + "-debounced")
    except Exception:
        dev.close()
        raise
    print(f"scroll_fix on {PATH} ({dev.name}) required={REQUIRED} idle={IDLE_RESET}", flush=True)
    try:
        dev.grab()
        try:
            for e in dev.read_loop():
                if e.type != ecodes.EV_SYN:
                    buf.append(e)
                    continue
                now = time.monotonic()
                vd = pkt_dir(buf, REL_V)
                hd = pkt_dir(buf, REL_H)
                v_held = [b for b in buf if b.type == ecodes.EV_REL and b.code in REL_V]
                h_held = [b for b in buf if b.type == ecodes.EV_REL and b.code in REL_H]
                v_act = v.decide(vd, now, v_held) if vd != 0 else "emit"
                h_act = h.decide(hd, now, h_held) if hd != 0 else "emit"

                out = []
                v_flush = v_act[1] if isinstance(v_act, tuple) else None
                h_flush = h_act[1] if isinstance(h_act, tuple) else None
                if v_flush is not None:
                    out.extend(v_flush)
                if h_flush is not None:
                    out.extend(h_flush)
                for b in buf:
                    is_v = b.type == ecodes.EV_REL and b.code in REL_V
                    is_h = b.type == ecodes.EV_REL and b.code in REL_H
                    if not is_v and not is_h:
                        out.append(b)
                    elif is_v and v_act == "emit":
                        out.append(b)
                    elif is_h and h_act == "emit":
                        out.append(b)
                    # hold / flush: скрол цієї осі вже в pending_buf / flushed — пропускаємо
                if out:
                    for b in out:
                        c = clamp(b)
                        ui.write(c.type, c.code, c.value)
                    ui.syn()
                buf = []
        except KeyboardInterrupt:
            pass
    finally:
        try:
            dev.ungrab()
        except Exception:
            pass
        try:
            ui.close()
        except Exception:
            pass
        dev.close()

if __name__ == "__main__":
    while True:
        try:
            run_once()
            break
        except KeyboardInterrupt:
            break
        except OSError:
            time.sleep(2)
        except Exception:
            time.sleep(2)
