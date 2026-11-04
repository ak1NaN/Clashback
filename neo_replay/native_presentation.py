"""Native spectator perspective and HUD refresh without restarting the scene.

Verified against boot-decrypted x86_64 builds 160402020 and 160635001. These methods only
refresh presentation; the snapshot owns battle state and the command queue.
Call on the game's update thread, before/after the target's one refresh tick.
"""
import ctypes
import json


def select_replay_view(g, bindings, replay_text, owner):
    """Select the bottom player before the native replay/HUD is constructed.

    C1EF00 copies a LogicLong spectator ID. D74F6B..D74F8F loads the JSON
    accountID.hi/lo into that layout in this order. No battle data is changed.
    """
    if bindings.build not in ("160402020", "160635001"):
        raise ValueError("Spectator selection is only verified for supported builds")
    if type(owner) is not int or owner not in (0, 1):
        raise ValueError("Spectator owner must be 0 or 1")
    avatar = json.loads(replay_text)["battle"][f"avatar{owner}"]
    words = [avatar[f"accountID.{part}"] for part in ("hi", "lo")]
    if any(type(word) is not int or not -(1 << 31) <= word < (1 << 32) for word in words):
        raise ValueError("Invalid native spectator account ID")
    identity = (ctypes.c_int32 * 2)(*words)
    g.call(bindings.fn("replay_set_spectator"), ctypes.addressof(identity), restype=None)


class SnapshotPresentation:
    def __init__(self, g, bindings):
        if bindings.build not in ("160402020", "160635001"):
            raise ValueError("Snapshot HUD refresh is only verified for supported builds")
        self.g, self.B = g, bindings

    @staticmethod
    def pointer(address):
        return ctypes.c_uint64.from_address(address).value

    def hud(self):
        view = self.g.call(self.B.fn("replay_spectator_view"))
        hud = self.pointer(view + 0x300) if view else 0
        if not hud:
            raise RuntimeError("Native spectator HUD is unavailable during snapshot restore")
        return hud

    def stop_banner(self, hud):
        clip = self.pointer(hud + 0x230)
        if clip:
            # Stop the old 30-second/elixir banner recursively. The next normal
            # HUD update hides it; ownership stays with the original renderer.
            self.g.call(self.B.fn("replay_stop_clip"), clip, restype=None)

    def restored(self):
        hud = self.hud()
        self.g.call(self.B.fn("replay_hud_reverse"), hud, restype=None)  # stock reverse-time HUD cleanup
        # Native notification drain. The bool is an integer argument, dt a float.
        self.g.call(self.B.fn("replay_hud_notifications"), hud, 1, ctypes.c_float(0.0), restype=None)
        self.g.call(self.B.fn("replay_hud_rebind"), hud, restype=None)  # rebind crowns to restored towers
        self.g.call(self.B.fn("replay_hud_notifications"), hud, 0, ctypes.c_float(0.05), restype=None)
        self.stop_banner(hud)

    def refreshed(self):
        # The target tick can create a transition from the previous time phase.
        # Clear that one transition too, then leave future animation untouched.
        hud = self.hud()
        self.g.call(self.B.fn("replay_hud_notifications"), hud, 1, ctypes.c_float(0.0), restype=None)
        self.stop_banner(hud)
