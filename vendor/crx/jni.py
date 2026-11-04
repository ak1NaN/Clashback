"""A stand-in for Java (the node has none): a JavaVM and a JNIEnv whose 233 standard JNI functions are Python callbacks.

The engine talks to Java through JNI at start-up (registering its native methods, asking for paths and device details). Every
call is LOGGED by name with what it asked for (class, method, string), so we see exactly what the engine needs; the answers come
from ANSWERS, and anything not answered returns 0/null and is still logged. Nothing is silently ignored.
"""
import ctypes

import loader as L
import os
import sys

from loader import KEEP

NAMES = ['reserved0', 'reserved1', 'reserved2', 'reserved3', 'GetVersion', 'DefineClass', 'FindClass', 'FromReflectedMethod',
         'FromReflectedField', 'ToReflectedMethod', 'GetSuperclass', 'IsAssignableFrom', 'ToReflectedField', 'Throw', 'ThrowNew',
         'ExceptionOccurred', 'ExceptionDescribe', 'ExceptionClear', 'FatalError', 'PushLocalFrame', 'PopLocalFrame', 'NewGlobalRef',
         'DeleteGlobalRef', 'DeleteLocalRef', 'IsSameObject', 'NewLocalRef', 'EnsureLocalCapacity', 'AllocObject', 'NewObject',
         'NewObjectV', 'NewObjectA', 'GetObjectClass', 'IsInstanceOf', 'GetMethodID']
TYPES = ['Object', 'Boolean', 'Byte', 'Char', 'Short', 'Int', 'Long', 'Float', 'Double', 'Void']
NAMES += [f'Call{t}Method{v}' for t in TYPES for v in ('', 'V', 'A')]
NAMES += [f'CallNonvirtual{t}Method{v}' for t in TYPES for v in ('', 'V', 'A')]
NAMES += ['GetFieldID'] + [f'Get{t}Field' for t in TYPES[:-1]] + [f'Set{t}Field' for t in TYPES[:-1]]
NAMES += ['GetStaticMethodID'] + [f'CallStatic{t}Method{v}' for t in TYPES for v in ('', 'V', 'A')]
NAMES += ['GetStaticFieldID'] + [f'GetStatic{t}Field' for t in TYPES[:-1]] + [f'SetStatic{t}Field' for t in TYPES[:-1]]
NAMES += ['NewString', 'GetStringLength', 'GetStringChars', 'ReleaseStringChars', 'NewStringUTF', 'GetStringUTFLength',
          'GetStringUTFChars', 'ReleaseStringUTFChars', 'GetArrayLength', 'NewObjectArray', 'GetObjectArrayElement',
          'SetObjectArrayElement']
PRIMS = TYPES[1:-1]
NAMES += [f'New{t}Array' for t in PRIMS] + [f'Get{t}ArrayElements' for t in PRIMS] + [f'Release{t}ArrayElements' for t in PRIMS]
NAMES += [f'Get{t}ArrayRegion' for t in PRIMS] + [f'Set{t}ArrayRegion' for t in PRIMS]
NAMES += ['RegisterNatives', 'UnregisterNatives', 'MonitorEnter', 'MonitorExit', 'GetJavaVM', 'GetStringRegion', 'GetStringUTFRegion',
          'GetPrimitiveArrayCritical', 'ReleasePrimitiveArrayCritical', 'GetStringCritical', 'ReleaseStringCritical',
          'NewWeakGlobalRef', 'DeleteWeakGlobalRef', 'ExceptionCheck', 'NewDirectByteBuffer', 'GetDirectBufferAddress',
          'GetDirectBufferCapacity', 'GetObjectRefType']
assert len(NAMES) == 233, len(NAMES)

LOG: list = []                       # every JNI call: (function, details)


class Jvm:
    """Handles: Java objects are small numbers mapped to Python values (('class', name), ('string', text), ...)."""

    def __init__(self, answers: 'dict | None' = None, verbose: bool = False):
        self.answers = answers or {}            # 'Class.method(sig)' -> value (int, str, or callable(args) -> value)
        self.verbose = verbose or bool(os.environ.get('CRX_JNI'))
        self.objects: dict = {}
        self.ids: dict = {}         # method/field ids -> (class, name, signature, static)
        self.natives: dict = {}       # 'Class.method(sig)' -> native function address (from RegisterNatives)
        self.strings: dict = {}
        self._next = 0x1000
        self.env_table = (ctypes.c_void_p * 233)()
        self.env = ctypes.c_void_p(ctypes.addressof(self.env_table))
        self.env_ptr = ctypes.pointer(self.env)                   # JNIEnv* = pointer to a pointer to the table
        for i, name in enumerate(NAMES):
            self.env_table[i] = self._make(name)
        self.vm_table = (ctypes.c_void_p * 8)()
        self.vm = ctypes.c_void_p(ctypes.addressof(self.vm_table))
        self.vm_ptr = ctypes.pointer(self.vm)
        def get_env(_vm, out, _version):
            ctypes.c_void_p.from_address(out).value = ctypes.addressof(self.env)
            return 0
        vm_fns = {3: ('DestroyJavaVM', lambda *_: 0), 4: ('AttachCurrentThread', get_env), 5: ('DetachCurrentThread', lambda *_: 0),
                  6: ('GetEnv', get_env), 7: ('AttachCurrentThreadAsDaemon', get_env)}
        for i, (name, fn) in vm_fns.items():
            self.vm_table[i] = L.callback(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(fn)
        KEEP.extend([self, self.env_table, self.vm_table])

    # --- objects ---------------------------------------------------------------------------------------------------------
    def new(self, kind: str, value) -> int:
        self._next += 8
        self.objects[self._next] = (kind, value)
        return self._next

    def string(self, text: str) -> int:
        return self.new('string', text)

    def describe(self, handle) -> str:
        kind, value = self.objects.get(handle or 0, ('null', None))
        return f'{kind}:{value}' if kind != 'null' else 'null'

    # --- the JNI functions -------------------------------------------------------------------------------------------------
    def _make(self, name: str) -> int:
        fn = getattr(self, '_' + name, None) or (lambda *args, _n=name: self._default(_n, args))
        return L.callback(ctypes.c_long, *[ctypes.c_void_p] * 6)(lambda *a, _fn=fn: _fn(*a) or 0)

    def _note(self, name: str, details: str = '') -> None:
        LOG.append((name, details))
        if self.verbose:
            sys.stderr.write(f'jni {name} {details}\n')

    def _default(self, name, args):
        details = ''
        if name.startswith(('Call', 'Get', 'Set')) and ('Method' in name or 'Field' in name) and len(args) > 2:
            target = self.ids.get(args[2] or 0)
            key = f'{target[0]}.{target[1]}{target[2]}' if target else f'id {args[2]}'
            details = f'{key} on {self.describe(args[1])}'
            answer = self.answers.get(key)
            self._note(name, details + ('' if answer is not None else '   (no answer: 0/null)'))
            if callable(answer):
                answer = answer(args)
            if answer is None:
                return 0
            if isinstance(answer, str):
                return self.string(answer)
            return int(answer or 0)
        self._note(name, details)
        return 0

    def _GetVersion(self, *_):
        return 0x10006

    def _FindClass(self, _env, name, *_):
        cls = ctypes.string_at(name).decode()
        self._note('FindClass', cls)
        return self.new('class', cls)

    def _GetObjectClass(self, _env, obj, *_):
        kind, value = self.objects.get(obj or 0, ('null', None))
        cls = value if kind == 'class' else (kind if kind not in ('string',) else 'java/lang/String')
        return self.new('class', cls)

    def _method_id(self, static, _env, cls, name, sig):
        c = self.objects.get(cls or 0, ('class', '?'))[1]
        n, s = ctypes.string_at(name).decode(), ctypes.string_at(sig).decode()
        self._next += 8
        self.ids[self._next] = (c, n, s, static)
        self._note('GetStaticMethodID' if static else 'GetMethodID', f'{c}.{n}{s}')
        return self._next

    def _GetMethodID(self, env, cls, name, sig, *_):
        return self._method_id(False, env, cls, name, sig)

    def _GetStaticMethodID(self, env, cls, name, sig, *_):
        return self._method_id(True, env, cls, name, sig)

    def _GetFieldID(self, env, cls, name, sig, *_):
        return self._method_id(False, env, cls, name, sig)

    def _GetStaticFieldID(self, env, cls, name, sig, *_):
        return self._method_id(True, env, cls, name, sig)

    def _NewStringUTF(self, _env, chars, *_):
        return self.string(ctypes.string_at(chars).decode(errors='replace') if chars else '')

    def _GetStringUTFChars(self, _env, s, is_copy, *_):
        kind, value = self.objects.get(s or 0, ('null', ''))
        buf = ctypes.create_string_buffer((value or '').encode())
        self.strings[ctypes.addressof(buf)] = buf
        if is_copy:
            ctypes.c_ubyte.from_address(is_copy).value = 1
        return ctypes.addressof(buf)

    def _GetStringUTFLength(self, _env, s, *_):
        return len((self.objects.get(s or 0, ('null', ''))[1] or '').encode())

    def _GetStringLength(self, _env, s, *_):
        return len(self.objects.get(s or 0, ('null', ''))[1] or '')

    def _ReleaseStringUTFChars(self, _env, _s, chars, *_):
        self.strings.pop(chars or 0, None)
        return 0

    def _RegisterNatives(self, _env, cls, methods, count, *_):
        c = self.objects.get(cls or 0, ('class', '?'))[1]
        for i in range(count):
            name_p, sig_p, fn = (ctypes.c_void_p * 3).from_address(methods + i * 24)
            key = f'{c}.{ctypes.string_at(name_p).decode()}{ctypes.string_at(sig_p).decode()}'
            self.natives[key] = fn
        self._note('RegisterNatives', f'{c}: {count} methods')
        return 0

    def _NewGlobalRef(self, _env, obj, *_):
        return obj or 0

    def _NewLocalRef(self, _env, obj, *_):
        return obj or 0

    def _NewWeakGlobalRef(self, _env, obj, *_):
        return obj or 0

    def _IsSameObject(self, _env, a, b, *_):
        return int((a or 0) == (b or 0))

    def _GetJavaVM(self, _env, out, *_):
        ctypes.c_void_p.from_address(out).value = ctypes.addressof(self.vm)
        return 0

    def _ExceptionCheck(self, *_):
        return 0

    def _ExceptionOccurred(self, *_):
        return 0

    def _PushLocalFrame(self, *_):
        return 0

    def _EnsureLocalCapacity(self, *_):
        return 0

    def _GetArrayLength(self, _env, arr, *_):
        kind, value = self.objects.get(arr or 0, ('null', None))
        self._note('GetArrayLength', self.describe(arr))
        return len(value) if isinstance(value, (list, bytes)) else 0

    def _FatalError(self, _env, msg, *_):
        text = ctypes.string_at(msg).decode(errors='replace') if msg else ''
        self._note('FatalError', text)
        raise SystemExit(f'the engine called JNI FatalError: {text}')


def _utf16(text: str):
    data = (text or '').encode('utf-16-le') + b'\0\0'
    return ctypes.create_string_buffer(data, len(data))


def _get_string_chars(self, _env, s, is_copy, *_):
    buf = _utf16(self.objects.get(s or 0, ('null', ''))[1])
    self.strings[ctypes.addressof(buf)] = buf
    if is_copy:
        ctypes.c_ubyte.from_address(is_copy).value = 1
    self._note('GetStringChars', self.describe(s))
    return ctypes.addressof(buf)


def _release_string_chars(self, _env, _s, chars, *_):
    self.strings.pop(chars or 0, None)
    return 0


def _get_string_region(self, _env, s, start, length, out, *_):
    text = (self.objects.get(s or 0, ('null', ''))[1] or '')[start:start + length]
    data = text.encode('utf-16-le')
    ctypes.memmove(out, data, len(data))
    return 0


def _get_string_utf_region(self, _env, s, start, length, out, *_):
    text = (self.objects.get(s or 0, ('null', ''))[1] or '')[start:start + length]
    data = text.encode()
    ctypes.memmove(out, data, len(data))
    ctypes.c_char.from_address(out + len(data)).value = b'\0'
    return 0


for _name, _fn in (('_GetStringChars', _get_string_chars), ('_ReleaseStringChars', _release_string_chars),
                   ('_GetStringCritical', _get_string_chars), ('_ReleaseStringCritical', _release_string_chars),
                   ('_GetStringRegion', _get_string_region), ('_GetStringUTFRegion', _get_string_utf_region)):
    setattr(Jvm, _name, _fn)


def va_args(va_list: int, count: int) -> list:
    """The first `count` integer/pointer arguments of an x86_64 va_list (the JNI ...MethodV functions get their Java arguments
    this way): {gp_offset u32, fp_offset u32, overflow_arg_area, reg_save_area}."""
    gp = ctypes.c_uint32.from_address(va_list).value
    overflow = ctypes.c_uint64.from_address(va_list + 8).value
    saved = ctypes.c_uint64.from_address(va_list + 16).value
    out = []
    for _ in range(count):
        if gp < 48:
            out.append(ctypes.c_uint64.from_address(saved + gp).value)
            gp += 8
        else:
            out.append(ctypes.c_uint64.from_address(overflow).value)
            overflow += 8
    return out
