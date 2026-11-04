"""The game draws straight into the Mac window: each frame the game swaps is copied on the GPU (one blit, flipped to top-down)
into one of two IOSurfaces, memory the GPU and the window's process share, and the window shows that surface as its picture
(viewer/Viewer.swift: a layer whose contents is the surface). No pixel goes through the CPU: no glReadPixels, no image built
per frame. ANGLE's EGL_ANGLE_iosurface_client_buffer makes the surface a pbuffer bound as a texture, attached to our own
framebuffer; the blit runs at eglSwapBuffers with the game's context current, and the GL state it touches is put back.

frame.shm's header carries the two surfaces' global ids (+52, +56) and the one last finished (+60, 0 or 1); the frame
is published only after glFinish, and the next frame goes to the other surface, so the window never shows a half-drawn one.

    CRX_LIVE_SURFACE=1 (live_mac.sh sets it; off: the frame is read back into frame.shm as before)
"""
import ctypes

P, I, U = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint
_io = ctypes.CDLL('/System/Library/Frameworks/IOSurface.framework/IOSurface')
_cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
_io.IOSurfaceCreate.restype, _io.IOSurfaceCreate.argtypes = P, [P]
_io.IOSurfaceGetID.restype, _io.IOSurfaceGetID.argtypes = U, [P]
_cf.CFDictionaryCreateMutable.restype, _cf.CFDictionaryCreateMutable.argtypes = P, [P, I, P, P]
_cf.CFNumberCreate.restype, _cf.CFNumberCreate.argtypes = P, [P, I, P]
_cf.CFStringCreateWithCString.restype, _cf.CFStringCreateWithCString.argtypes = P, [P, ctypes.c_char_p, U]
_cf.CFDictionarySetValue.argtypes = [P, P, P]

EGL_IOSURFACE_ANGLE, EGL_IOSURFACE_PLANE_ANGLE, EGL_TEXTURE_INTERNAL_FORMAT_ANGLE, EGL_TEXTURE_TYPE_ANGLE = 0x3454, 0x345A, 0x345D, 0x345C
EGL_TEXTURE_TARGET, EGL_TEXTURE_FORMAT, EGL_TEXTURE_RGBA, EGL_TEXTURE_2D, EGL_BACK_BUFFER = 0x3081, 0x3080, 0x305E, 0x305F, 0x3084
GL_TEXTURE_2D, GL_FRAMEBUFFER, GL_READ_FRAMEBUFFER, GL_DRAW_FRAMEBUFFER = 0x0DE1, 0x8D40, 0x8CA8, 0x8CA9
GL_READ_FRAMEBUFFER_BINDING, GL_DRAW_FRAMEBUFFER_BINDING, GL_TEXTURE_BINDING_2D = 0x8CAA, 0x8CA6, 0x8069
GL_SCISSOR_TEST, GL_RASTERIZER_DISCARD, GL_COLOR_BUFFER_BIT, GL_NEAREST, GL_BGRA_EXT, GL_UNSIGNED_BYTE = 0x0C11, 0x8C89, 0x4000, 0x2600, 0x80E1, 0x1401


def _packed(values):
    return (ctypes.c_int * (len(values) + 1))(*values, 0x3038)


def global_surface(w, h):
    """An IOSurface (BGRA, 8 bits) any process can open by its id."""
    d = _cf.CFDictionaryCreateMutable(None, 0, ctypes.addressof(P.in_dll(_cf, 'kCFTypeDictionaryKeyCallBacks')),
                                      ctypes.addressof(P.in_dll(_cf, 'kCFTypeDictionaryValueCallBacks')))
    key = lambda k: _cf.CFStringCreateWithCString(None, k, 0x08000100)   # noqa: E731
    for k, v in ((b'IOSurfaceWidth', w), (b'IOSurfaceHeight', h), (b'IOSurfaceBytesPerElement', 4),
                 (b'IOSurfacePixelFormat', 0x42475241)):                  # 'BGRA'
        x = ctypes.c_int64(v)
        _cf.CFDictionarySetValue(d, key(k), _cf.CFNumberCreate(None, 4, ctypes.byref(x)))
    _cf.CFDictionarySetValue(d, key(b'IOSurfaceIsGlobal'), P.in_dll(_cf, 'kCFBooleanTrue'))
    return _io.IOSurfaceCreate(d)


class SurfaceOut:
    def __init__(self, EB, w, h):
        self.EB, self.w, self.h = EB, w, h
        egl, gl = EB.egl, EB.gles
        for f, r, a in (('eglCreatePbufferFromClientBuffer', P, [P, U, P, P, P]), ('eglBindTexImage', U, [P, P, I]),
                        ('eglGetCurrentDisplay', P, [])):
            getattr(egl, f).restype, getattr(egl, f).argtypes = r, a
        gl.glBlitFramebuffer.argtypes = [I] * 8 + [U, U]
        dpy = egl.eglGetCurrentDisplay()
        cfg, n = P(), I()
        egl.eglChooseConfig(dpy, _packed([0x3033, 1, 0x3040, 0x40, 0x3024, 8, 0x3023, 8, 0x3022, 8, 0x3021, 8]),
                            ctypes.byref(cfg), 1, ctypes.byref(n))
        self.surfaces, self.ids, self.fbos = [], [], []
        old_tex = I()
        gl.glGetIntegerv(GL_TEXTURE_BINDING_2D, ctypes.byref(old_tex))
        old_draw = I()
        gl.glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, ctypes.byref(old_draw))
        for _ in range(2):
            s = global_surface(w, h)
            pb = egl.eglCreatePbufferFromClientBuffer(dpy, EGL_IOSURFACE_ANGLE, s, cfg, _packed([
                0x3057, w, 0x3056, h, EGL_IOSURFACE_PLANE_ANGLE, 0, EGL_TEXTURE_TARGET, EGL_TEXTURE_2D,
                EGL_TEXTURE_INTERNAL_FORMAT_ANGLE, GL_BGRA_EXT, EGL_TEXTURE_FORMAT, EGL_TEXTURE_RGBA,
                EGL_TEXTURE_TYPE_ANGLE, GL_UNSIGNED_BYTE]))
            if not pb:
                raise RuntimeError(f'IOSurface pbuffer failed: {hex(egl.eglGetError())}')
            tex, fbo = U(), U()
            gl.glGenTextures(1, ctypes.byref(tex))
            gl.glBindTexture(GL_TEXTURE_2D, tex)
            if not egl.eglBindTexImage(dpy, pb, EGL_BACK_BUFFER):
                raise RuntimeError(f'eglBindTexImage failed: {hex(egl.eglGetError())}')
            gl.glGenFramebuffers(1, ctypes.byref(fbo))
            gl.glBindFramebuffer(GL_DRAW_FRAMEBUFFER, fbo)
            gl.glFramebufferTexture2D(GL_DRAW_FRAMEBUFFER, 0x8CE0, GL_TEXTURE_2D, tex, 0)
            self.surfaces.append((s, pb, tex))
            self.ids.append(_io.IOSurfaceGetID(s))
            self.fbos.append(fbo.value)
        gl.glBindTexture(GL_TEXTURE_2D, old_tex.value)
        gl.glBindFramebuffer(GL_DRAW_FRAMEBUFFER, old_draw.value)
        self.next = 0
        self.state = [I(), I()]

    def blit(self):
        """The default framebuffer (the frame just drawn) -> the next surface, top row first. -> that surface's index."""
        gl = self.EB.gles
        rd, dr = self.state
        gl.glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, ctypes.byref(rd))
        gl.glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, ctypes.byref(dr))
        scissor = gl.glIsEnabled(GL_SCISSOR_TEST)
        discard = gl.glIsEnabled(GL_RASTERIZER_DISCARD)
        if scissor:
            gl.glDisable(GL_SCISSOR_TEST)
        if discard:
            gl.glDisable(GL_RASTERIZER_DISCARD)
        k = self.next
        gl.glBindFramebuffer(GL_READ_FRAMEBUFFER, 0)
        gl.glBindFramebuffer(GL_DRAW_FRAMEBUFFER, self.fbos[k])
        gl.glBlitFramebuffer(0, 0, self.w, self.h, 0, self.h, self.w, 0, GL_COLOR_BUFFER_BIT, GL_NEAREST)
        gl.glFinish()
        gl.glBindFramebuffer(GL_READ_FRAMEBUFFER, rd.value)
        gl.glBindFramebuffer(GL_DRAW_FRAMEBUFFER, dr.value)
        if scissor:
            gl.glEnable(GL_SCISSOR_TEST)
        if discard:
            gl.glEnable(GL_RASTERIZER_DISCARD)
        self.next = 1 - k
        return k
