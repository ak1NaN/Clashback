"""The engine's window-system calls (EGL, ANativeWindow) connected to Mesa's software EGL in the container.

On a phone the game draws into an Android window. Here there is no window: every surface the engine asks for becomes an
off-screen buffer of the same size (a pbuffer), configs asked for windows are matched against pbuffer configs, and each
eglSwapBuffers (one finished frame) can be read back and saved. Every OpenGL ES function the engine uses it fetches through
eglGetProcAddress, which goes to Mesa as it is (same calling convention on x86_64 Linux).

    import egl_bridge as EB; EB.install(width=720, height=1600, out='/out/frames', every=30)   # before loading libg.so
"""
import ctypes
import os
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import loader as L  # noqa: E402

RTLD_GLOBAL = ctypes.RTLD_GLOBAL
MAC = sys.platform == 'darwin'
if MAC:                     # macOS: ANGLE (OpenGL ES on Metal, the Mac's GPU); CRX_ANGLE = the folder with its two libraries
    _angle = Path(os.environ.get('CRX_ANGLE', Path.home() / '.cache/crx-render/angle'))
    gles = ctypes.CDLL(str(_angle / 'libGLESv2.dylib'), mode=RTLD_GLOBAL)
    egl = ctypes.CDLL(str(_angle / 'libEGL.dylib'), mode=RTLD_GLOBAL)
else:                       # Linux: Mesa (llvmpipe, drawn on the CPU)
    egl = ctypes.CDLL('libEGL.so.1', mode=RTLD_GLOBAL)
    gles = ctypes.CDLL('libGLESv2.so.2', mode=RTLD_GLOBAL)
P = ctypes.c_void_p
for f, res, args in (('eglGetPlatformDisplay', P, [ctypes.c_uint, P, P]), ('eglInitialize', ctypes.c_uint, [P, P, P]),
                     ('eglChooseConfig', ctypes.c_uint, [P, P, P, ctypes.c_int, P]),
                     ('eglGetConfigs', ctypes.c_uint, [P, P, ctypes.c_int, P]),
                     ('eglGetConfigAttrib', ctypes.c_uint, [P, P, ctypes.c_int, P]),
                     ('eglCreateContext', P, [P, P, P, P]), ('eglCreatePbufferSurface', P, [P, P, P]),
                     ('eglDestroySurface', ctypes.c_uint, [P, P]), ('eglMakeCurrent', ctypes.c_uint, [P, P, P, P]),
                     ('eglSwapBuffers', ctypes.c_uint, [P, P]), ('eglGetCurrentContext', P, []),
                     ('eglGetProcAddress', P, [ctypes.c_char_p]), ('eglGetError', ctypes.c_int, [])):
    fn = getattr(egl, f)
    fn.restype, fn.argtypes = res, args
gles.glReadPixels.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, P]

EGL_NONE, EGL_SURFACE_TYPE, EGL_WINDOW_BIT, EGL_PBUFFER_BIT = 0x3038, 0x3033, 0x0004, 0x0001
EGL_WIDTH, EGL_HEIGHT, EGL_PLATFORM_SURFACELESS_MESA = 0x3057, 0x3056, 0x31DD


def _platform_display():
    """The display every surface and context lives on: Mesa's surfaceless platform, or ANGLE on Metal."""
    if MAC:
        get = ctypes.CFUNCTYPE(P, ctypes.c_uint, P, P)(egl.eglGetProcAddress(b'eglGetPlatformDisplayEXT'))
        return get(0x3202, None, _packed([0x3203, 0x3489]))         # EGL_PLATFORM_ANGLE_ANGLE, type Metal
    return egl.eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, None, None)

LOG: list = []                    # (seconds since install, thread name, call, details)
STATE = {'width': 720, 'height': 1600, 'out': None, 'every': 30, 'frames': 0, 'saved': [], 't0': time.time(),
         'display': None, 'surfaces': {}, 'procs': set()}
WINDOW = ctypes.create_string_buffer(256)       # the stand-in ANativeWindow the engine gets (never read by Mesa)


def log(call, details=''):
    LOG.append((round(time.time() - STATE['t0'], 3), threading.current_thread().name, call, details))
    if os.environ.get('CRX_EGL_TRACE'):
        sys.stderr.write(f'egl: {call} {details}\n')


def _attribs(ptr):
    out, i = [], 0
    if not ptr:
        return out
    a = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_int))
    while a[i] != EGL_NONE and i < 128:
        out += [a[i], a[i + 1]]
        i += 2
    return out


def _packed(pairs):
    return (ctypes.c_int * (len(pairs) + 1))(*pairs, EGL_NONE)


def png(path, w, h, rgba):
    rows = b''.join(b'\0' + rgba[(h - 1 - y) * w * 4:(h - y) * w * 4] for y in range(h))     # GL rows are bottom-up
    chunk = lambda t, d: struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)  # noqa: E731
    Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
                           + chunk(b'IDAT', zlib.compress(rows, 3)) + chunk(b'IEND', b''))


def save_frame(name=None):
    w, h = STATE['width'], STATE['height']
    buf = ctypes.create_string_buffer(w * h * 4)
    gles.glReadPixels(0, 0, w, h, 0x1908, 0x1401, buf)
    if STATE['out']:
        path = Path(STATE['out']) / (name or f'frame_{STATE["frames"]:05d}.png')
        png(path, w, h, buf.raw)
        STATE['saved'].append(str(path))
    return buf.raw


def no_sound(libg_path):
    """FMOD (sound) is not loaded. Its stand-ins returned 0, which is FMOD_OK: the game then believed banks and events
    loaded while every output pointer stayed unset. Every FMOD function now reports an error (FMOD_ERR_UNINITIALIZED, 67)
    and the game runs without sound. Silent and counted (loader.STUBS_CALLED)."""
    from elf import Elf
    names = {s['name'] for s in Elf(str(libg_path)).symbols if not s['defined'] and 'FMOD' in s['name']}
    for name in names:
        bool_like = name.endswith(('isValidEv', 'hasSustainPointEPb'))
        L.stub(name, 0 if bool_like else 67)
    return len(names)


def install(width=720, height=1600, out=None, every=30):
    STATE.update(width=width, height=height, out=out, every=every, fps_cap=float(os.environ.get('CRX_RENDER_FPS', '0')))
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)

    @L.shim('eglGetDisplay', P, P)
    def _get_display(native_display):
        if not STATE['display']:
            STATE['display'] = _platform_display()
        log('eglGetDisplay', hex(native_display or 0))
        return STATE['display']

    @L.shim('eglInitialize', ctypes.c_uint, P, P, P)
    def _init(dpy, major, minor):
        r = egl.eglInitialize(dpy, major, minor)
        log('eglInitialize', f'-> {r}')
        return r

    def pbuffer_pairs(pairs):
        out = list(pairs)
        for i in range(0, len(out), 2):
            if out[i] == EGL_SURFACE_TYPE:
                out[i + 1] = (out[i + 1] & ~EGL_WINDOW_BIT) | EGL_PBUFFER_BIT
        if EGL_SURFACE_TYPE not in out[0::2]:                       # absent means "windows" (EGL's default): ask for pbuffers
            out += [EGL_SURFACE_TYPE, EGL_PBUFFER_BIT]
        return out

    @L.shim('eglChooseConfig', ctypes.c_uint, P, P, P, ctypes.c_int, P)
    def _choose(dpy, attrib, configs, size, num):
        pairs = _attribs(attrib)
        fixed = pbuffer_pairs(pairs)
        r = egl.eglChooseConfig(dpy, _packed(fixed), configs, size, num)
        n = ctypes.cast(num, ctypes.POINTER(ctypes.c_int))[0] if num else -1
        log('eglChooseConfig', f'{[hex(x) for x in pairs]} -> {r}, {n} configs')
        return r

    @L.shim('eglGetConfigs', ctypes.c_uint, P, P, ctypes.c_int, P)
    def _configs(dpy, configs, size, num):
        r = egl.eglGetConfigs(dpy, configs, size, num)
        log('eglGetConfigs', f'size {size} -> {r}')
        return r

    @L.shim('eglGetConfigAttrib', ctypes.c_uint, P, P, ctypes.c_int, P)
    def _cfg_attr(dpy, config, attribute, value):
        r = egl.eglGetConfigAttrib(dpy, config, attribute, value)
        if attribute == EGL_SURFACE_TYPE and r and value:          # say it can do windows: every window becomes a pbuffer
            v = ctypes.cast(value, ctypes.POINTER(ctypes.c_int))
            v[0] |= EGL_WINDOW_BIT
        return r

    @L.shim('eglCreateContext', P, P, P, P, P)
    def _ctx(dpy, config, share, attrib):
        if not share and STATE.get('boot_context'):             # share objects made during setup on our boot context
            share = STATE['boot_context']
        if MAC and os.environ.get('CRX_ANGLE_NO_ERROR', '1') == '1':     # ANGLE: skip checking every GL call for errors
            attrib = _packed(_attribs(attrib) + [0x31B3, 1])                          # EGL_CONTEXT_OPENGL_NO_ERROR_KHR
        r = egl.eglCreateContext(dpy, config, share, attrib)
        log('eglCreateContext', f'{[hex(x) for x in _attribs(attrib)]} -> {hex(r or 0)} err 0x{egl.eglGetError():x}')
        return r

    @L.shim('eglCreateWindowSurface', P, P, P, P, P)
    def _window(dpy, config, window, attrib):
        r = egl.eglCreatePbufferSurface(dpy, config, _packed([EGL_WIDTH, STATE['width'], EGL_HEIGHT, STATE['height']]))
        log('eglCreateWindowSurface', f'window {hex(window or 0)} -> pbuffer {hex(r or 0)} {STATE["width"]}x{STATE["height"]}'
            f' err 0x{egl.eglGetError():x}')
        return r

    @L.shim('eglDestroySurface', ctypes.c_uint, P, P)
    def _destroy(dpy, surface):
        log('eglDestroySurface', hex(surface or 0))
        return egl.eglDestroySurface(dpy, surface)

    @L.shim('eglMakeCurrent', ctypes.c_uint, P, P, P, P)
    def _current(dpy, draw, read, ctx):
        r = egl.eglMakeCurrent(dpy, draw, read, ctx)
        log('eglMakeCurrent', f'draw {hex(draw or 0)} ctx {hex(ctx or 0)} -> {r}')
        return r

    timing = {'hook': 0.0, 'swap': 0.0, 'between': 0.0, 'last_end': None, 'n': 0} if os.environ.get('CRX_SWAP_TIMING') else None

    @L.shim('eglSwapBuffers', ctypes.c_uint, P, P)
    def _swap(dpy, surface):
        t_in = time.perf_counter()
        if timing is not None and timing['last_end'] is not None:
            timing['between'] += t_in - timing['last_end']
        r = _swap_body(dpy, surface, t_in)
        if timing is not None:
            timing['last_end'] = time.perf_counter()
            timing['n'] += 1
            if timing['n'] == 300:
                log('frame time', ' '.join(f'{k} {timing[k] / 3:.2f} ms' for k in ('between', 'hook', 'swap')) + ' (per frame)')
                timing.update(hook=0.0, swap=0.0, between=0.0, n=0)
        return r

    def _swap_body(dpy, surface, t_in):
        STATE['frames'] += 1
        n = STATE['frames']
        hook = STATE.get('on_frame')
        if hook:
            try:
                hook(n)
            except Exception as e:                                     # noqa: BLE001
                log('on_frame failed', repr(e))
        live = STATE.get('live')
        if live:
            try:
                live.write(n)
            except Exception as e:                                     # noqa: BLE001
                log('live write failed', repr(e))
        if STATE['out'] and STATE['every'] and (n <= 3 or n % STATE['every'] == 0):
            save_frame(STATE.get('frame_name', lambda k: None)(n))
        if n <= 3 or n % 300 == 0:
            log('eglSwapBuffers', f'frame {n}')
        t_swap = time.perf_counter()
        if timing is not None:
            timing['hook'] += t_swap - t_in
        r = egl.eglSwapBuffers(dpy, surface)
        if timing is not None:
            timing['swap'] += time.perf_counter() - t_swap
        cap = STATE.get('fps_cap')
        if cap:                                    # a window would wait for the screen; a pbuffer does not: pace it here
            now = time.time()
            due = STATE.get('next_frame', now)
            if due > now:
                time.sleep(due - now)
            STATE['next_frame'] = max(due, now) + 1.0 / cap
        return r

    @L.shim('eglGetCurrentContext', P)
    def _get_ctx():
        return egl.eglGetCurrentContext()

    @L.shim('eglGetError', ctypes.c_int)
    def _err():
        return egl.eglGetError()

    WRAP = _gl_wrappers() if os.environ.get('CRX_EGL_SHADER_LOG') else {}   # Python on every compile: diagnosis only

    counting = os.environ.get('CRX_GL_COUNT') == '1'          # diagnosis: calls per GL function (a machine-code counter each)

    @L.shim('eglGetProcAddress', P, ctypes.c_char_p)
    def _proc(name):
        key = name.decode(errors='replace')
        if key in WRAP:
            return WRAP[key]
        r = egl.eglGetProcAddress(name)
        if counting and r:
            return _counted(key, r)
        if os.environ.get('CRX_GL_NODRAW') == '1' and key.startswith(('glDrawElements', 'glDrawArrays')):   # diagnosis only
            if 'nodraw' not in STATE:
                STATE['nodraw'] = L.alloc_exec(4096)
                ctypes.memmove(STATE['nodraw'], b'\xc3', 1)                    # ret: the draw does nothing
            return STATE['nodraw']
        STATE['procs'].add(name.decode(errors='replace'))
        if not r:
            log('eglGetProcAddress', f'{name.decode(errors="replace")} -> NULL')
        return r

    @L.shim('ANativeWindow_fromSurface', P, P, P)
    def _from_surface(env, surface):
        log('ANativeWindow_fromSurface', hex(surface or 0))
        return ctypes.addressof(WINDOW)

    @L.shim('ANativeWindow_release', None, P)
    def _release(window):
        log('ANativeWindow_release', hex(window or 0))


GL_COUNTS: dict = {}                       # name -> address of its call counter (CRX_GL_COUNT)
_COUNT_PAGE = {'data': None, 'code': None, 'used': 0}


def _counted(name, real):
    """A stub that adds one to the function's counter and jumps to it (rax is free: GL functions take no variable arguments).
    Counters and code on separate pages (a write next to translated code costs a re-translation under Rosetta)."""
    import struct
    if name in GL_COUNTS:
        return GL_COUNTS[name][1]
    if _COUNT_PAGE['data'] is None or _COUNT_PAGE['used'] >= 4096 // 32:
        buf = ctypes.create_string_buffer(8192)
        L.KEEP.append(buf)
        _COUNT_PAGE.update(data=(ctypes.addressof(buf) + 4095) & ~4095, code=L.alloc_exec(4096 * 2), used=0)
    k = _COUNT_PAGE['used']
    counter, stub = _COUNT_PAGE['data'] + 8 * k, _COUNT_PAGE['code'] + 32 * k
    code = b'\x48\xb8' + struct.pack('<Q', counter) + bytes.fromhex('f048ff00') + b'\xff\x25\x00\x00\x00\x00' + struct.pack('<Q', real)
    ctypes.memmove(stub, code, len(code))
    _COUNT_PAGE['used'] += 1
    GL_COUNTS[name] = (counter, stub)
    return stub


def gl_counts():
    return {n: ctypes.c_uint64.from_address(c).value for n, (c, _s) in GL_COUNTS.items()}


def _gl_wrappers():
    """Shader compile / link calls, logged (source size, status, the driver's message): rare calls, so a Python hop is fine."""
    real = lambda n, res, *args: ctypes.CFUNCTYPE(res, *args)(egl.eglGetProcAddress(n.encode()))  # noqa: E731
    U, I = ctypes.c_uint, ctypes.c_int
    shader_source = real('glShaderSource', None, U, I, P, P)
    compile_ = real('glCompileShader', None, U)
    get_shader = real('glGetShaderiv', None, U, U, P)
    shader_log = real('glGetShaderInfoLog', None, U, I, P, P)
    link = real('glLinkProgram', None, U)
    get_prog = real('glGetProgramiv', None, U, U, P)
    prog_log = real('glGetProgramInfoLog', None, U, I, P, P)
    sources = {}
    out = {}

    def text_of(count, strings, lengths):
        parts = []
        arr = ctypes.cast(strings, ctypes.POINTER(ctypes.c_char_p))
        lens = ctypes.cast(lengths, ctypes.POINTER(ctypes.c_int)) if lengths else None
        for i in range(count):
            if lens and lens[i] >= 0:
                parts.append(ctypes.string_at(ctypes.cast(strings, ctypes.POINTER(ctypes.c_void_p))[i], lens[i]))
            else:
                parts.append(arr[i] or b'')
        return b''.join(parts)

    @L.callback(None, U, I, P, P)
    def _src(shader, count, strings, lengths):
        sources[shader] = text_of(count, strings, lengths)
        shader_source(shader, count, strings, lengths)
    out['glShaderSource'] = _src

    @L.callback(None, U)
    def _compile(shader):
        compile_(shader)
        ok = ctypes.c_int()
        get_shader(shader, 0x8B81, ctypes.byref(ok))                    # COMPILE_STATUS
        src = sources.get(shader, b'')
        if not ok.value:
            buf = ctypes.create_string_buffer(4096)
            shader_log(shader, 4096, None, buf)
            log('glCompileShader FAILED', f'shader {shader}, {len(src)} bytes: {buf.value.decode(errors="replace")[:600]}')
            if STATE['out']:
                Path(STATE['out'], f'failed_shader_{shader}.glsl').write_bytes(src)
        else:
            STATE.setdefault('compiled', 0)
            STATE['compiled'] += 1
            if STATE['compiled'] <= 3:
                log('glCompileShader ok', f'shader {shader}, {len(src)} bytes')
    out['glCompileShader'] = _compile

    @L.callback(None, U)
    def _link(program):
        link(program)
        ok = ctypes.c_int()
        get_prog(program, 0x8B82, ctypes.byref(ok))                      # LINK_STATUS
        if not ok.value:
            buf = ctypes.create_string_buffer(4096)
            prog_log(program, 4096, None, buf)
            log('glLinkProgram FAILED', f'program {program}: {buf.value.decode(errors="replace")[:600]}')
    out['glLinkProgram'] = _link
    return out


def make_boot_context(es=3):
    """A context current on the calling thread before the game's setup runs (on a phone, setup runs where graphics are live).
    The game's own contexts later share its objects (shaders, buffers, textures)."""
    dpy = STATE['display'] or _platform_display()
    STATE['display'] = dpy
    egl.eglInitialize(dpy, None, None)
    cfg, n = ctypes.c_void_p(), ctypes.c_int()
    egl.eglChooseConfig(dpy, _packed([EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, 0x3040, 0x40, 0x3024, 8, 0x3023, 8, 0x3022, 8, 0x3021, 8,
                                      0x3025, 24, 0x3026, 8]), ctypes.byref(cfg), 1, ctypes.byref(n))
    surf = egl.eglCreatePbufferSurface(dpy, cfg, _packed([EGL_WIDTH, STATE['width'], EGL_HEIGHT, STATE['height']]))
    ctx = egl.eglCreateContext(dpy, cfg, None, _packed([0x3098, es] + ([0x31B3, 1] if MAC and os.environ.get('CRX_ANGLE_NO_ERROR', '1') == '1' else [])))
    ok = egl.eglMakeCurrent(dpy, surf, surf, ctx)
    STATE['boot_context'] = ctx
    log('boot context', f'{hex(ctx or 0)} current {ok}')
    return ctx


def release_boot_context():
    """Make no context current on the calling thread once setup is done (the boot context stays alive: objects it made are
    shared with the game's contexts)."""
    if STATE.get('display'):
        gles.glFinish()                    # submit and finish what setup drew on it: otherwise the render thread can wait on
        r = egl.eglMakeCurrent(STATE['display'], None, None, None)       # work that was never flushed (stalls)
        log('boot context', f'released {r}')
