/* bionic_mac: the parts of Android's C library (bionic, x86_64) that macOS does differently, for the Clash Royale engine running
natively on a Mac (an x86_64 process under Rosetta; crx/mac/README.md).

The engine (libg.so) and its C++ runtime (libc++_shared.so) are Android x86_64 code. A Mac x86_64 process uses the same calling
convention (System V, the same va_list), so most C library functions connect to macOS's own directly (crx/loader.py). This
library has the others, exported as a_<android name>: different structure layouts (stat, dirent, pthread objects, mbstate),
different constants (open flags, mmap flags, clocks, sysconf, locale categories), Android-only functions (fortify _chk,
__sF, __errno, emutls, futex through syscall) and network functions the simulation never uses. Modelled on crx/win.

    crx/mac/build.sh  ->  ~/.cache/crx-render/mac/bionic_mac.dylib (x86_64)
*/
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <dirent.h>
#include <locale.h>
#include <xlocale.h>
#include <math.h>
#include <time.h>
#include <pthread.h>
#include <sched.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <sys/statvfs.h>
#include <sys/time.h>
#include <malloc/malloc.h>
#include <os/os_sync_wait_on_address.h>
#include <dlfcn.h>
#include <wchar.h>
#include <pthread/qos.h>

#define API(ret, name, ...) __attribute__((visibility("default"))) ret a_##name(__VA_ARGS__)
#define DATA(type, name) __attribute__((visibility("default"))) type a_##name
typedef long long i64;
typedef unsigned long long u64;
typedef int32_t wchar_a;                       /* Android's wchar_t (4 bytes, as macOS's) */

#define A_ETIMEDOUT 110
#define A_ENOSYS 38
#define A_EBUSY 16
#define A_EINVAL 22
#define A_ENOMEM 12
#define A_EAGAIN 11

static void bio_log(const char *fmt, ...) {
    if (!getenv("CRX_BIONIC_LOG")) return;
    va_list ap; va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap); fflush(stderr);
}

/* ---- errno: macOS's own (numbers 1-34 are the same as Linux; above differ, e.g. EAGAIN 35 vs 11) --------------------------- */
API(int *, __errno, void) { return __error(); }

/* ---- memory: fresh blocks are zeroed (as crx/win: the engine leaves some fields of new objects unwritten; on Linux they read 0) */
API(void *, malloc, size_t n) { return calloc(1, n ? n : 1); }
API(void *, realloc, void *p, size_t n) {
    size_t old = p ? malloc_size(p) : 0;
    void *q = realloc(p, n ? n : 1);
    if (q) { size_t now = malloc_size(q); if (now > old) memset((char *)q + old, 0, now - old); }
    return q;
}
API(int, posix_memalign, void **out, size_t align, size_t n) {
    int r = posix_memalign(out, align < 16 ? 16 : align, n ? n : 1); if (!r) memset(*out, 0, n ? n : 1); return r ? A_ENOMEM : 0;
}

/* ---- fortify (_chk) variants and other Android-only string functions -------------------------------------------------- */
API(void *, __memcpy_chk, void *d, const void *s, size_t n, size_t dn) { (void)dn; return memcpy(d, s, n); }
API(void *, __memmove_chk, void *d, const void *s, size_t n, size_t dn) { (void)dn; return memmove(d, s, n); }
API(void *, __memset_chk, void *d, int c, size_t n, size_t dn) { (void)dn; return memset(d, c, n); }
API(char *, __strcpy_chk, char *d, const char *s, size_t dn) { (void)dn; return strcpy(d, s); }
API(char *, __strcat_chk, char *d, const char *s, size_t dn) { (void)dn; return strcat(d, s); }
API(char *, __strncpy_chk, char *d, const char *s, size_t n, size_t dn) { (void)dn; return strncpy(d, s, n); }
API(char *, __strncpy_chk2, char *d, const char *s, size_t n, size_t dn, size_t sn) { (void)dn; (void)sn; return strncpy(d, s, n); }
API(size_t, __strlen_chk, const char *s, size_t sn) { (void)sn; return strlen(s); }
API(char *, __strchr_chk, const char *s, int c, size_t sn) { (void)sn; return strchr(s, c); }
API(char *, __strrchr_chk, const char *s, int c, size_t sn) { (void)sn; return strrchr(s, c); }
API(i64, __read_chk, int fd, void *buf, size_t n, size_t bn) { (void)bn; return read(fd, buf, n); }
API(char *, __gnu_strerror_r, int e, char *buf, size_t n) { strerror_r(e, buf, n); return buf; }
API(int, __vsnprintf_chk, char *buf, size_t n, int flag, size_t bn, const char *fmt, va_list ap) { (void)flag; (void)bn; return vsnprintf(buf, n, fmt, ap); }
API(int, __vsprintf_chk, char *buf, int flag, size_t bn, const char *fmt, va_list ap) { (void)flag; return vsnprintf(buf, bn, fmt, ap); }
API(void, sincosf, float x, float *s, float *c) { *s = sinf(x); *c = cosf(x); }

/* ---- stdio: Android's __sF (three 152-byte FILE structures the engine only passes back) -> macOS's stdin/stdout/stderr ---- */
DATA(unsigned char, __sF)[3 * 152];
DATA(void *, stderr) = a___sF + 2 * 152;
static FILE *F(void *f) {
    if ((unsigned char *)f >= a___sF && (unsigned char *)f < a___sF + sizeof a___sF) {
        int k = (int)(((unsigned char *)f - a___sF) / 152);
        return k == 0 ? stdin : k == 1 ? stdout : stderr;
    }
    return (FILE *)f;
}
API(int, fclose, void *f) { return fclose(F(f)); }
API(size_t, fread, void *p, size_t s, size_t n, void *f) { return fread(p, s, n, F(f)); }
API(size_t, fwrite, const void *p, size_t s, size_t n, void *f) { return fwrite(p, s, n, F(f)); }
API(size_t, __fwrite_chk, const void *p, size_t s, size_t n, void *f, size_t bn) { (void)bn; return fwrite(p, s, n, F(f)); }
API(int, fseek, void *f, long off, int w) { return fseek(F(f), off, w); }
API(int, fseeko, void *f, i64 off, int w) { return fseeko(F(f), off, w); }
API(long, ftell, void *f) { return ftell(F(f)); }
API(i64, ftello, void *f) { return ftello(F(f)); }
API(int, fflush, void *f) { return fflush(f ? F(f) : NULL); }
API(char *, fgets, char *s, int n, void *f) { return fgets(s, n, F(f)); }
API(int, fputs, const char *s, void *f) { return fputs(s, F(f)); }
API(int, fputc, int c, void *f) { return fputc(c, F(f)); }
API(int, getc, void *f) { return getc(F(f)); }
API(int, ungetc, int c, void *f) { return ungetc(c, F(f)); }
API(int, ferror, void *f) { return ferror(F(f)); }
API(int, fileno, void *f) { return fileno(F(f)); }
API(unsigned, fputwc, unsigned c, void *f) { return fputwc((wchar_t)c, F(f)); }
API(unsigned, getwc, void *f) { return getwc(F(f)); }
API(unsigned, ungetwc, unsigned c, void *f) { return ungetwc((wint_t)c, F(f)); }
API(int, vfprintf, void *f, const char *fmt, va_list ap) { return vfprintf(F(f), fmt, ap); }
API(int, fprintf, void *f, const char *fmt, ...) { va_list ap; va_start(ap, fmt); int r = vfprintf(F(f), fmt, ap); va_end(ap); return r; }

/* ---- files: Linux flag numbers -> macOS ---------------------------------------------------------------------------------- */
static int mac_oflags(int f) {
    int m = f & 3;
    if (f & 0x40) m |= O_CREAT;      if (f & 0x80) m |= O_EXCL;       if (f & 0x100) m |= O_NOCTTY;
    if (f & 0x200) m |= O_TRUNC;     if (f & 0x400) m |= O_APPEND;    if (f & 0x800) m |= O_NONBLOCK;
    if (f & 0x10000) m |= O_DIRECTORY; if (f & 0x20000) m |= O_NOFOLLOW; if (f & 0x80000) m |= O_CLOEXEC;
    if (f & 0x101000) m |= O_SYNC;
    return m;
}
static int lx_oflags(int m) {
    int f = m & 3;
    if (m & O_APPEND) f |= 0x400; if (m & O_NONBLOCK) f |= 0x800; if (m & O_SYNC) f |= 0x101000;
    return f;
}
static int mac_dirfd(int d) { return d == -100 ? AT_FDCWD : d; }
static int mac_atflags(int f) {
    int m = 0;
    if (f & 0x100) m |= AT_SYMLINK_NOFOLLOW;
    if (f & 0x200) m |= AT_REMOVEDIR;                         /* (unlinkat; AT_EACCESS shares the number elsewhere) */
    return m;
}
API(int, open, const char *path, int flags, ...) {
    va_list ap; va_start(ap, flags); int mode = (flags & 0x40) ? va_arg(ap, int) : 0; va_end(ap);
    int fd = open(path, mac_oflags(flags), mode);
    bio_log("crx: open(%s, 0x%x) -> %d\n", path, flags, fd);
    return fd;
}
API(int, openat, int dirfd, const char *path, int flags, ...) {
    va_list ap; va_start(ap, flags); int mode = (flags & 0x40) ? va_arg(ap, int) : 0; va_end(ap);
    return openat(mac_dirfd(dirfd), path, mac_oflags(flags), mode);
}
API(int, fcntl, int fd, int cmd, ...) {
    va_list ap; va_start(ap, cmd); long arg = va_arg(ap, long); va_end(ap);
    switch (cmd) {
    case 0: return fcntl(fd, F_DUPFD, (int)arg);
    case 1: return fcntl(fd, F_GETFD);
    case 2: return fcntl(fd, F_SETFD, (int)arg);
    case 3: { int m = fcntl(fd, F_GETFL); return m < 0 ? m : lx_oflags(m); }
    case 4: return fcntl(fd, F_SETFL, mac_oflags((int)arg) & ~3);
    case 1030: return fcntl(fd, F_DUPFD_CLOEXEC, (int)arg);
    }
    bio_log("crx: fcntl cmd %d\n", cmd); errno = EINVAL; return -1;
}
API(i64, lseek64, int fd, i64 off, int whence) { return lseek(fd, off, whence); }
API(int, unlinkat, int d, const char *path, int flags) { return unlinkat(mac_dirfd(d), path, mac_atflags(flags)); }
API(int, fchmodat, int d, const char *p, unsigned m, int f) { return fchmodat(mac_dirfd(d), p, (mode_t)m, mac_atflags(f) & AT_SYMLINK_NOFOLLOW); }
API(int, utimensat, int d, const char *p, const struct timespec *t, int f) { return utimensat(mac_dirfd(d), p, t, mac_atflags(f) & AT_SYMLINK_NOFOLLOW); }
API(i64, pathconf, const char *p, int n) { (void)p; (void)n; return 255; }
API(i64, sendfile, int a, int b, i64 *o, size_t n) { (void)a; (void)b; (void)o; (void)n; errno = ENOSYS; return -1; }

/* struct stat: the x86_64 kernel layout bionic uses (144 bytes) */
typedef struct { u64 dev, ino, nlink; unsigned mode, uid, gid, pad; u64 rdev; i64 size, blksize, blocks;
                 i64 atime, atime_ns, mtime, mtime_ns, ctime, ctime_ns; i64 reserved[3]; } bst_stat;
static int fill_stat(bst_stat *o, const struct stat *s, int r) {
    if (r) return r;
    memset(o, 0, sizeof *o);
    o->dev = (u64)s->st_dev; o->ino = s->st_ino; o->nlink = s->st_nlink; o->mode = s->st_mode; o->uid = s->st_uid; o->gid = s->st_gid;
    o->rdev = (u64)s->st_rdev; o->size = s->st_size; o->blksize = s->st_blksize; o->blocks = s->st_blocks;
    o->atime = s->st_atimespec.tv_sec; o->atime_ns = s->st_atimespec.tv_nsec; o->mtime = s->st_mtimespec.tv_sec;
    o->mtime_ns = s->st_mtimespec.tv_nsec; o->ctime = s->st_ctimespec.tv_sec; o->ctime_ns = s->st_ctimespec.tv_nsec;
    return 0;
}
API(int, stat, const char *path, bst_stat *out) { struct stat s; int r = stat(path, &s); return fill_stat(out, &s, r); }
API(int, lstat, const char *path, bst_stat *out) { struct stat s; int r = lstat(path, &s); return fill_stat(out, &s, r); }
API(int, fstat, int fd, bst_stat *out) { struct stat s; int r = fstat(fd, &s); return fill_stat(out, &s, r); }
typedef struct { u64 bsize, frsize, blocks, bfree, bavail, files, ffree, favail, fsid, flag, namemax; u64 spare[6]; } bst_statvfs;
API(int, statvfs, const char *p, bst_statvfs *o) {
    struct statvfs s; if (statvfs(p, &s)) return -1;
    memset(o, 0, sizeof *o); o->bsize = s.f_bsize; o->frsize = s.f_frsize; o->blocks = s.f_blocks; o->bfree = s.f_bfree;
    o->bavail = s.f_bavail; o->files = s.f_files; o->ffree = s.f_ffree; o->favail = s.f_favail; o->fsid = s.f_fsid;
    o->flag = s.f_flag; o->namemax = s.f_namemax; return 0;
}

/* folders: Android's struct dirent {u64 ino; i64 off; u16 reclen; u8 type; char name[256]} */
typedef struct { u64 ino; i64 off; unsigned short reclen; unsigned char type; char name[256]; } bst_dirent;
typedef struct { DIR *d; bst_dirent ent; } bst_dir;
static bst_dir *wrap_dir(DIR *d) { if (!d) return NULL; bst_dir *w = calloc(1, sizeof *w); w->d = d; return w; }
API(void *, opendir, const char *path) { return wrap_dir(opendir(path)); }
API(void *, fdopendir, int fd) { return wrap_dir(fdopendir(fd)); }
API(bst_dirent *, readdir, bst_dir *w) {
    struct dirent *e = readdir(w->d);
    if (!e) return NULL;
    memset(&w->ent, 0, sizeof w->ent);
    w->ent.ino = e->d_ino; w->ent.off = (i64)e->d_seekoff; w->ent.reclen = sizeof w->ent; w->ent.type = e->d_type;
    strncpy(w->ent.name, e->d_name, 255);
    return &w->ent;
}
API(int, closedir, bst_dir *w) { if (!w) return -1; int r = closedir(w->d); free(w); return r; }

/* ---- memory mapping: Linux flags -> macOS (MAP_ANONYMOUS 0x20 -> 0x1000, MAP_NORESERVE 0x4000 -> 0x40) ------------------ */
API(void *, mmap, void *addr, size_t len, int prot, int flags, int fd, i64 off) {
    int m = flags & (MAP_SHARED | MAP_PRIVATE | MAP_FIXED);
    if (flags & 0x20) m |= MAP_ANON;
    if (flags & 0x4000) m |= MAP_NORESERVE;
    return mmap(addr, len, prot, m, (flags & 0x20) ? -1 : fd, off);
}
API(void *, mremap, void *old, size_t olen, size_t nlen, int flags, ...) {
    if (nlen <= olen) return old;
    if (!(flags & 1)) { errno = ENOMEM; return (void *)-1; }        /* MREMAP_MAYMOVE */
    void *p = mmap(NULL, nlen, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (p == MAP_FAILED) return p;
    memcpy(p, old, olen); munmap(old, olen); return p;
}
API(int, madvise, void *a, size_t n, int adv) { (void)a; (void)n; (void)adv; return 0; }
API(int, mlock, const void *a, size_t n) { (void)a; (void)n; return 0; }

/* ---- time: Linux clock numbers -> macOS ----------------------------------------------------------------------------------- */
static clockid_t mac_clock(int c) {
    switch (c) {
    case 0: case 5: case 8: return CLOCK_REALTIME;                              /* REALTIME, _COARSE, _ALARM */
    case 2: return CLOCK_PROCESS_CPUTIME_ID;
    case 3: return CLOCK_THREAD_CPUTIME_ID;
    case 4: return CLOCK_MONOTONIC_RAW;
    default: return CLOCK_MONOTONIC;                                           /* MONOTONIC, _COARSE, BOOTTIME */
    }
}
/* The game's clocks can run faster for a while (crx_time_warp): the live viewer's rewind restarts the battle, and the game's
   start-of-battle intro (a 3 s overlay, the VS banner, the camera) runs on clock time, not on battle time (the banner on the
   wall clock). Battle ticks do not follow it (the viewer feeds them exactly). Every clock stays continuous and moves by the
   same extra amount. Timed waits below use the same virtual time, so their deadlines agree. */
static pthread_mutex_t g_warp_lock = PTHREAD_MUTEX_INITIALIZER;
static double g_warp = 1.0;
static i64 g_extra0, g_mono0;                    /* virtual = real + extra; extra grows by (k - 1) x steady time passed */
static int warped(clockid_t c) { return c == CLOCK_MONOTONIC || c == CLOCK_MONOTONIC_RAW || c == CLOCK_REALTIME; }
static int steady(clockid_t c) { return c == CLOCK_MONOTONIC || c == CLOCK_MONOTONIC_RAW; }
static i64 real_ns(clockid_t c) { struct timespec t; clock_gettime(c, &t); return (i64)t.tv_sec * 1000000000ll + t.tv_nsec; }
static i64 extra_now(void) {                     /* call with the lock held */
    return g_warp == 1.0 ? g_extra0 : g_extra0 + (i64)((double)(real_ns(CLOCK_MONOTONIC) - g_mono0) * (g_warp - 1.0));
}
static i64 virt_ns(clockid_t c) {
    i64 r = real_ns(c);
    if (!warped(c)) return r;
    pthread_mutex_lock(&g_warp_lock);
    i64 v = r + extra_now();
    pthread_mutex_unlock(&g_warp_lock);
    return v;
}
__attribute__((visibility("default"))) void crx_time_warp(double k) {
    pthread_mutex_lock(&g_warp_lock);
    g_extra0 = extra_now(); g_mono0 = real_ns(CLOCK_MONOTONIC); g_warp = k > 0 ? k : 1.0;
    pthread_mutex_unlock(&g_warp_lock);
}
__attribute__((visibility("default"))) double crx_steady_seconds(void) { return virt_ns(CLOCK_MONOTONIC) / 1e9; }
static long g_clock_calls[16];
__attribute__((visibility("default"))) long crx_clock_calls(int c) { return c >= 0 && c < 16 ? g_clock_calls[c] : 0; }
API(int, gettimeofday, struct timeval *tv, void *tz) {
    (void)tz; i64 v = virt_ns(CLOCK_REALTIME); tv->tv_sec = v / 1000000000ll; tv->tv_usec = (int)(v % 1000000000ll / 1000); return 0;
}
API(i64, time, i64 *t) { i64 v = virt_ns(CLOCK_REALTIME) / 1000000000ll; if (t) *t = v; return v; }
API(int, clock_gettime, int c, struct timespec *ts) {
    if (c >= 0 && c < 16) __atomic_add_fetch(&g_clock_calls[c], 1, __ATOMIC_RELAXED);
    clockid_t m = mac_clock(c);
    if (!warped(m)) return clock_gettime(m, ts);
    i64 v = virt_ns(m); ts->tv_sec = v / 1000000000ll; ts->tv_nsec = v % 1000000000ll; return 0;
}
static i64 now_ns(int c) { return virt_ns(mac_clock(c)); }

/* ---- sysconf with Android's numbers ---------------------------------------------------------------------------------------- */
API(long, sysconf, int name) {
    switch (name) {
    case 0x27: case 0x28: return 4096;                                         /* _SC_PAGESIZE / PAGE_SIZE */
    case 0x60: return sysconf(_SC_NPROCESSORS_CONF);
    case 0x61: return sysconf(_SC_NPROCESSORS_ONLN);
    case 0x62: return sysconf(_SC_PHYS_PAGES);
    case 0x63: return sysconf(_SC_PHYS_PAGES) / 2;                             /* available pages: an estimate */
    case 0x6: return 100;                                                      /* _SC_CLK_TCK */
    case 0x5: return sysconf(_SC_OPEN_MAX);
    }
    bio_log("crx: sysconf(%d) unknown\n", name);
    return -1;
}

/* ---- process ----------------------------------------------------------------------------------------------------------------- */
API(void, __assert2, const char *file, int line, const char *fn, const char *msg) {
    fprintf(stderr, "crx: engine assertion %s:%d %s: %s\n", file, line, fn ? fn : "", msg); fflush(stderr); abort();
}
API(void, __stack_chk_fail, void) { fprintf(stderr, "crx: engine stack check failed\n"); fflush(stderr); abort(); }
API(int, __cxa_atexit, void *fn, void *arg, void *dso) { (void)fn; (void)arg; (void)dso; return 0; }   /* never run: the process ends */
API(void, __cxa_finalize, void *dso) { (void)dso; }
API(int, __cxa_thread_atexit_impl, void *fn, void *obj, void *dso) { (void)fn; (void)obj; (void)dso; return 0; }
API(int, __register_atfork, void *a, void *b, void *c, void *d) { (void)a; (void)b; (void)c; (void)d; return 0; }
API(int, gettid, void) { u64 t; pthread_threadid_np(NULL, &t); return (int)t; }
API(size_t, __ctype_get_mb_cur_max, void) { return 4; }        /* bionic's C locale is UTF-8 */
API(int, __system_property_get, const char *name, char *value) { (void)name; value[0] = 0; return 0; }

/* ---- threads -------------------------------------------------------------------------------------------------------------- */
typedef void *(*start_fn)(void *);
API(int, pthread_create, pthread_t *out, const void *attr, start_fn fn, void *arg) {
    (void)attr;                                   /* Android's attribute layout differs; the engine sets no attributes it needs */
    pthread_attr_t a; pthread_attr_init(&a); pthread_attr_setstacksize(&a, 8 << 20);   /* macOS's default (512 KB) is small */
    int r = pthread_create(out, &a, fn, arg); pthread_attr_destroy(&a);
    return r ? A_EAGAIN : 0;
}
API(int, pthread_getschedparam, pthread_t t, int *policy, int *param) { (void)t; *policy = 0; *param = 0; return 0; }
API(int, pthread_setschedparam, pthread_t t, int policy, const int *param) { (void)t; (void)policy; (void)param; return 0; }
API(int, sched_get_priority_min, int p) { (void)p; return 0; }
API(int, pthread_sigmask, int how, const void *s, void *o) { (void)how; (void)s; if (o) memset(o, 0, 8); return 0; }
API(int, sigfillset, u64 *s) { *s = ~0ull; return 0; }

/* mutex (Android: 40 bytes; the initializers only set the first int: 0x4000 = recursive): a macOS mutex made on first use, its
   pointer at +8 */
typedef struct { int state; int pad; pthread_mutex_t *mx; char rest[24]; } a_mutex;
static pthread_mutex_t *mx_of(a_mutex *m) {
    pthread_mutex_t *mx = __atomic_load_n(&m->mx, __ATOMIC_ACQUIRE);
    if (!mx) {
        pthread_mutex_t *n = malloc(sizeof *n); pthread_mutexattr_t at; pthread_mutexattr_init(&at);
        if (m->state & 0x4000) pthread_mutexattr_settype(&at, PTHREAD_MUTEX_RECURSIVE);
        pthread_mutex_init(n, &at); pthread_mutexattr_destroy(&at);
        pthread_mutex_t *expected = NULL;
        if (__atomic_compare_exchange_n(&m->mx, &expected, n, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) mx = n;
        else { pthread_mutex_destroy(n); free(n); mx = expected; }
    }
    return mx;
}
API(int, pthread_mutex_init, a_mutex *m, const long *attr) { memset(m, 0, sizeof *m); if (attr && *attr == 1) m->state = 0x4000; return 0; }
API(int, pthread_mutex_destroy, a_mutex *m) { if (m->mx) { pthread_mutex_destroy(m->mx); free(m->mx); m->mx = NULL; } return 0; }
API(int, pthread_mutex_lock, a_mutex *m) { return pthread_mutex_lock(mx_of(m)); }
API(int, pthread_mutex_trylock, a_mutex *m) { return pthread_mutex_trylock(mx_of(m)) ? A_EBUSY : 0; }
API(int, pthread_mutex_unlock, a_mutex *m) { return pthread_mutex_unlock(mx_of(m)); }
API(int, pthread_mutexattr_init, long *a) { *a = 0; return 0; }
API(int, pthread_mutexattr_destroy, long *a) { (void)a; return 0; }
API(int, pthread_mutexattr_settype, long *a, int type) { *a = type; return 0; }

/* condition (Android: 48 bytes): the clock at +0 (from the attribute), a macOS condition made on first use at +8 */
typedef struct { int clock; int pad; pthread_cond_t *cv; char rest[32]; } a_cond;
static pthread_cond_t *cv_of(a_cond *c) {
    pthread_cond_t *cv = __atomic_load_n(&c->cv, __ATOMIC_ACQUIRE);
    if (!cv) {
        pthread_cond_t *n = malloc(sizeof *n); pthread_cond_init(n, NULL);
        pthread_cond_t *expected = NULL;
        if (__atomic_compare_exchange_n(&c->cv, &expected, n, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) cv = n;
        else { pthread_cond_destroy(n); free(n); cv = expected; }
    }
    return cv;
}
API(int, pthread_cond_init, a_cond *c, const long *attr) { memset(c, 0, sizeof *c); c->clock = attr ? (int)*attr : 0; return 0; }
API(int, pthread_cond_destroy, a_cond *c) { if (c->cv) { pthread_cond_destroy(c->cv); free(c->cv); c->cv = NULL; } return 0; }
API(int, pthread_cond_signal, a_cond *c) { return pthread_cond_signal(cv_of(c)); }
API(int, pthread_cond_broadcast, a_cond *c) { return pthread_cond_broadcast(cv_of(c)); }
API(int, pthread_cond_wait, a_cond *c, a_mutex *m) { return pthread_cond_wait(cv_of(c), mx_of(m)); }
API(int, pthread_cond_timedwait, a_cond *c, a_mutex *m, const struct timespec *abs) {
    i64 left = (i64)abs->tv_sec * 1000000000ll + abs->tv_nsec - now_ns(c->clock);    /* the deadline is on the condition's clock */
    if (steady(mac_clock(c->clock)) && g_warp != 1.0) left = (i64)(left / g_warp);   /* virtual time -> real time to wait */
    if (left < 0) left = 0;
    struct timespec rel = {left / 1000000000ll, left % 1000000000ll};
    int r = pthread_cond_timedwait_relative_np(cv_of(c), mx_of(m), &rel);
    return r == ETIMEDOUT ? A_ETIMEDOUT : r;
}
API(int, pthread_condattr_init, long *a) { *a = 0; return 0; }
API(int, pthread_condattr_destroy, long *a) { (void)a; return 0; }
API(int, pthread_condattr_setclock, long *a, int clock) { *a = clock; return 0; }

/* rwlock (Android: 56 bytes): a macOS rwlock made on first use at +8 */
typedef struct { int pad[2]; pthread_rwlock_t *rw; char rest[40]; } a_rwlock;
static pthread_rwlock_t *rw_of(a_rwlock *r) {
    pthread_rwlock_t *rw = __atomic_load_n(&r->rw, __ATOMIC_ACQUIRE);
    if (!rw) {
        pthread_rwlock_t *n = malloc(sizeof *n); pthread_rwlock_init(n, NULL);
        pthread_rwlock_t *expected = NULL;
        if (__atomic_compare_exchange_n(&r->rw, &expected, n, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) rw = n;
        else { pthread_rwlock_destroy(n); free(n); rw = expected; }
    }
    return rw;
}
API(int, pthread_rwlock_rdlock, a_rwlock *r) { return pthread_rwlock_rdlock(rw_of(r)); }
API(int, pthread_rwlock_wrlock, a_rwlock *r) { return pthread_rwlock_wrlock(rw_of(r)); }
API(int, pthread_rwlock_unlock, a_rwlock *r) { return pthread_rwlock_unlock(rw_of(r)); }

/* once (an int): 0 not run, 1 running, 2 done */
typedef void (*void_fn)(void);
API(int, pthread_once, volatile int *o, void_fn fn) {
    if (__atomic_load_n(o, __ATOMIC_ACQUIRE) == 2) return 0;
    int expected = 0;
    if (__atomic_compare_exchange_n(o, &expected, 1, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) { fn(); __atomic_store_n(o, 2, __ATOMIC_RELEASE); return 0; }
    while (__atomic_load_n(o, __ATOMIC_ACQUIRE) != 2) sched_yield();
    return 0;
}
/* keys: Android's are ints */
API(int, pthread_key_create, int *key, void (*destructor)(void *)) { pthread_key_t k; int r = pthread_key_create(&k, destructor); if (!r) *key = (int)k; return r ? A_EAGAIN : 0; }
API(int, pthread_key_delete, int key) { return pthread_key_delete((pthread_key_t)(unsigned)key); }
API(void *, pthread_getspecific, int key) { return pthread_getspecific((pthread_key_t)(unsigned)key); }
API(int, pthread_setspecific, int key, const void *v) { return pthread_setspecific((pthread_key_t)(unsigned)key, v); }

/* emulated thread-locals (Android x86_64 builds' thread_local): control = {size, align, index, template} */
typedef struct { u64 size, align, index; void *templ; } emu_ctl;
static u64 g_emu_next;
static pthread_key_t g_emu_key;
static pthread_once_t g_emu_once = PTHREAD_ONCE_INIT;
static void emu_init(void) { pthread_key_create(&g_emu_key, NULL); }
API(void *, __emutls_get_address, emu_ctl *c) {
    pthread_once(&g_emu_once, emu_init);
    u64 idx = __atomic_load_n(&c->index, __ATOMIC_ACQUIRE);
    if (!idx) {
        u64 n = __atomic_add_fetch(&g_emu_next, 1, __ATOMIC_ACQ_REL), expected = 0;
        idx = __atomic_compare_exchange_n(&c->index, &expected, n, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE) ? n : expected;
    }
    void **arr = pthread_getspecific(g_emu_key);
    size_t cap = arr ? (size_t)arr[0] : 0;
    if (idx >= cap) {
        size_t ncap = idx + 32; void **na = calloc(ncap + 1, sizeof(void *));
        if (arr) { memcpy(na, arr, (cap + 1) * sizeof(void *)); free(arr); }
        na[0] = (void *)ncap; arr = na; pthread_setspecific(g_emu_key, arr);
    }
    void *p = arr[idx];
    if (!p) {
        size_t al = c->align < 16 ? 16 : c->align;
        if (posix_memalign(&p, al, c->size ? c->size : 1)) return NULL;
        if (c->templ) memcpy(p, c->templ, c->size); else memset(p, 0, c->size);
        arr[idx] = p;
    }
    return p;
}

/* ---- syscall: what the engine and libc++ use (gettid, futex for atomic waits, getrandom) --------------------------------- */
API(i64, syscall, i64 nr, ...) {
    va_list ap; va_start(ap, nr);
    i64 r = -1;
    if (nr == 186) r = a_gettid();
    else if (nr == 202) {                                           /* futex(addr, op, val, timeout / count, addr2, val3) */
        void *addr = va_arg(ap, void *); int op = va_arg(ap, int) & 0x7f; int val = va_arg(ap, int);
        const struct timespec *ts = va_arg(ap, const struct timespec *);
        if (op == 0 || op == 9) {                                   /* WAIT (relative timeout) / WAIT_BITSET (absolute) */
            int rc;
            if (ts) {
                i64 ns = (i64)ts->tv_sec * 1000000000ll + ts->tv_nsec;
                if (op == 9) { ns -= now_ns(1); if (g_warp != 1.0) ns = (i64)(ns / g_warp); }
                if (ns <= 0) { errno = ETIMEDOUT; va_end(ap); return -1; }
                rc = os_sync_wait_on_address_with_timeout(addr, (uint64_t)(uint32_t)val, 4, OS_SYNC_WAIT_ON_ADDRESS_NONE,
                                                          OS_CLOCK_MACH_ABSOLUTE_TIME, (uint64_t)ns);
            } else rc = os_sync_wait_on_address(addr, (uint64_t)(uint32_t)val, 4, OS_SYNC_WAIT_ON_ADDRESS_NONE);
            r = rc < 0 ? -1 : 0;
        } else if (op == 1 || op == 10) {                           /* WAKE / WAKE_BITSET */
            if (val == 1) os_sync_wake_by_address_any(addr, 4, OS_SYNC_WAKE_BY_ADDRESS_NONE);   /* -1 = nobody waited: fine */
            else os_sync_wake_by_address_all(addr, 4, OS_SYNC_WAKE_BY_ADDRESS_NONE);
            r = 0;
        } else { bio_log("crx: futex op %d\n", op); errno = ENOSYS; }
    } else if (nr == 318) {                                         /* getrandom(buf, len, flags) */
        void *buf = va_arg(ap, void *); size_t len = va_arg(ap, size_t); arc4random_buf(buf, len); r = (i64)len;
    } else { bio_log("crx: syscall %lld\n", nr); errno = ENOSYS; }
    va_end(ap);
    return r;
}

/* ---- setjmp / longjmp (System V: rbx, rbp, r12-r15, rsp, return address; no signal mask) ---------------------------------- */
__asm__(
    ".globl _a_setjmp\n _a_setjmp:\n"
    "  mov %rbx, 0(%rdi)\n mov %rbp, 8(%rdi)\n mov %r12, 16(%rdi)\n mov %r13, 24(%rdi)\n mov %r14, 32(%rdi)\n mov %r15, 40(%rdi)\n"
    "  lea 8(%rsp), %rdx\n mov %rdx, 48(%rdi)\n mov (%rsp), %rdx\n mov %rdx, 56(%rdi)\n xor %eax, %eax\n ret\n"
    ".globl _a_longjmp\n _a_longjmp:\n"
    "  mov %esi, %eax\n test %eax, %eax\n jnz 1f\n inc %eax\n1:\n"
    "  mov 0(%rdi), %rbx\n mov 8(%rdi), %rbp\n mov 16(%rdi), %r12\n mov 24(%rdi), %r13\n mov 32(%rdi), %r14\n mov 40(%rdi), %r15\n"
    "  mov 48(%rdi), %rsp\n jmp *56(%rdi)\n");

/* ---- dynamic linking: the engine's own libraries are registered by the loader (C++ exception unwinding finds them here) --- */
typedef struct { u64 addr; const char *name; const void *phdr; unsigned short phnum; u64 adds, subs, tls_id; void *tls_data; } a_phdr_info;
static a_phdr_info g_mods[8]; static int g_nmods;
__attribute__((visibility("default"))) void crx_register_module(u64 base, const void *phdr, int phnum, const char *name) {
    if (g_nmods < 8) { a_phdr_info *m = &g_mods[g_nmods++]; memset(m, 0, sizeof *m); m->addr = base; m->phdr = phdr; m->phnum = (unsigned short)phnum; m->name = strdup(name); }
}
typedef int (*phdr_cb)(a_phdr_info *, size_t, void *);
API(int, dl_iterate_phdr, phdr_cb cb, void *data) {
    for (int k = 0; k < g_nmods; k++) { int r = cb(&g_mods[k], sizeof g_mods[k], data); if (r) return r; }
    return 0;
}
/* dlopen: the system C libraries are this library; any other Android library is absent. dlsym: our a_<name>, else macOS's. */
static void *g_self;
API(void *, dlopen, const char *name, int flags) {
    (void)flags;
    if (!g_self) { Dl_info i; dladdr((void *)a_dlopen, &i); g_self = dlopen(i.dli_fname, RTLD_NOW | RTLD_NOLOAD); }
    void *h = (!name || strstr(name, "libc.so") || strstr(name, "libm.so") || strstr(name, "libdl.so")) ? g_self : NULL;
    bio_log("crx: dlopen(%s) -> %p\n", name ? name : "(self)", h);
    return h;
}
API(void *, dlsym, void *h, const char *name) {
    (void)h; char buf[256]; snprintf(buf, sizeof buf, "a_%s", name);
    void *p = dlsym(RTLD_DEFAULT, buf); if (!p) p = dlsym(RTLD_DEFAULT, name);
    bio_log("crx: dlsym(%s) -> %p\n", name, p);
    return p;
}
API(int, dlclose, void *h) { (void)h; return 0; }
API(char *, dlerror, void) { return "crx: no dynamic libraries"; }

/* ---- locale: Linux category numbers / masks -> macOS; locale objects are macOS's (the *_l functions are macOS's own) ----- */
static const int CAT[7] = {LC_CTYPE, LC_NUMERIC, LC_TIME, LC_COLLATE, LC_MONETARY, LC_MESSAGES, LC_ALL};
API(char *, setlocale, int cat, const char *name) { return (cat >= 0 && cat <= 6) ? setlocale(CAT[cat], name) : NULL; }
API(void *, newlocale, int mask, const char *name, void *base) {
    int m = 0;
    if (mask & 1) m |= LC_CTYPE_MASK;   if (mask & 2) m |= LC_NUMERIC_MASK; if (mask & 4) m |= LC_TIME_MASK;
    if (mask & 8) m |= LC_COLLATE_MASK; if (mask & 16) m |= LC_MONETARY_MASK; if (mask & 32) m |= LC_MESSAGES_MASK;
    locale_t l = newlocale(m, name, (locale_t)base);
    if (!l) l = newlocale(m, "C", (locale_t)base);                 /* e.g. C.UTF-8, which macOS does not know */
    return l;
}

/* ---- multibyte <-> wide: UTF-8 with Android's 8-byte mbstate_t (macOS's is 128 bytes: never let it write into the engine's) */
static int utf8_dec(const unsigned char *s, size_t n, unsigned *cp) {
    if (!n) return -2; unsigned c = s[0];
    if (c < 0x80) { *cp = c; return c ? 1 : 0; }
    int len = c >= 0xf0 ? 4 : c >= 0xe0 ? 3 : c >= 0xc0 ? 2 : -1; if (len < 0) return -1; if ((size_t)len > n) return -2;
    unsigned v = c & (0xff >> (len + 1)); for (int k = 1; k < len; k++) v = v << 6 | (s[k] & 0x3f); *cp = v; return len;
}
static int utf8_enc(char *o, unsigned c) {
    if (c < 0x80) { o[0] = (char)c; return 1; } if (c < 0x800) { o[0] = 0xc0 | c >> 6; o[1] = 0x80 | (c & 63); return 2; }
    if (c < 0x10000) { o[0] = 0xe0 | c >> 12; o[1] = 0x80 | ((c >> 6) & 63); o[2] = 0x80 | (c & 63); return 3; }
    o[0] = 0xf0 | c >> 18; o[1] = 0x80 | ((c >> 12) & 63); o[2] = 0x80 | ((c >> 6) & 63); o[3] = 0x80 | (c & 63); return 4;
}
API(size_t, mbrtowc, wchar_a *w, const char *s, size_t n, void *st) { (void)st; if (!s) return 0; unsigned cp; int r = utf8_dec((const unsigned char *)s, n, &cp); if (r < 0) return (size_t)r; if (w) *w = cp; return r; }
API(size_t, mbrlen, const char *s, size_t n, void *st) { (void)st; unsigned cp; int r = utf8_dec((const unsigned char *)s, n, &cp); return (size_t)r; }
API(int, mbtowc, wchar_a *w, const char *s, size_t n) { if (!s) return 0; unsigned cp; int r = utf8_dec((const unsigned char *)s, n, &cp); if (r < 0) return -1; if (w) *w = cp; return r; }
API(size_t, wcrtomb, char *o, wchar_a c, void *st) { (void)st; if (!o) return 1; return utf8_enc(o, (unsigned)c); }
API(unsigned, btowc, int c) { return c >= 0 && c < 128 ? (unsigned)c : 0xffffffffu; }
API(int, wctob, unsigned c) { return c < 128 ? (int)c : -1; }
API(size_t, mbsnrtowcs, wchar_a *d, const char **src, size_t nms, size_t len, void *st) {
    (void)st; const unsigned char *s = (const unsigned char *)*src; size_t k = 0, used = 0;
    while (used < nms && (!d || k < len)) { unsigned cp; int r = utf8_dec(s + used, nms - used, &cp); if (r < 0) return (size_t)-1; if (r == 0) { if (d) { d[k] = 0; *src = NULL; } return k; } if (d) d[k] = cp; k++; used += r; }
    if (d) *src = (const char *)s + used; return k;
}
API(size_t, mbsrtowcs, wchar_a *d, const char **src, size_t len, void *st) { return a_mbsnrtowcs(d, src, (size_t)-1, len, st); }
API(size_t, wcsnrtombs, char *d, const wchar_a **src, size_t nwc, size_t len, void *st) {
    (void)st; const wchar_a *s = *src; size_t k = 0, used = 0; char b[4];
    while (used < nwc) { if (!s[used]) { if (d) { if (k < len) d[k] = 0; *src = NULL; } return k; }
        int n = utf8_enc(b, (unsigned)s[used]); if (d && k + n > len) break; if (d) memcpy(d + k, b, n); k += n; used++; }
    if (d) *src = s + used; return k;
}

/* ---- network and Linux-only event functions: the simulation never uses them --------------------------------------------- */
#define NOSYS(name, ...) API(int, name, __VA_ARGS__) { errno = ENOSYS; return -1; }
NOSYS(socket, int a, int b, int c) NOSYS(bind, int a, const void *b, unsigned c) NOSYS(connect, int a, const void *b, unsigned c)
NOSYS(shutdown, int a, int b) NOSYS(getpeername, int a, void *b, void *c) NOSYS(getsockname, int a, void *b, void *c)
NOSYS(getsockopt, int a, int b, int c, void *d, void *e) NOSYS(setsockopt, int a, int b, int c, const void *d, unsigned e)
NOSYS(epoll_create, int a) NOSYS(epoll_create1, int a) NOSYS(epoll_ctl, int a, int b, int c, void *d)
/* epoll_wait: the engine's network thread waits here for socket events, up to its next timer (up to 300 s). Answering "not
   supported" at once made that thread loop flat out (~300,000 lock/unlock pairs per frame: one core at 100%, found
   2026-10-13; crx/win/thread.c has the same fix). Offline nothing ever arrives: wait out the timeout (at most 100 ms, so the loop
   still runs its timers ten times a second) and report no events. */
API(int, epoll_wait, int epfd, void *events, int max, int timeout) {
    (void)epfd; (void)events; (void)max;
    int ms = timeout < 0 || timeout > 100 ? 100 : timeout;
    if (ms > 0) { struct timespec t = {0, (long)ms * 1000000}; nanosleep(&t, NULL); }
    return 0;
}
NOSYS(eventfd, unsigned a, int b) NOSYS(getifaddrs, void **a)
API(i64, recvfrom, int a, void *b, size_t c, int d, void *e, void *f) { errno = ENOSYS; return -1; }
API(i64, sendto, int a, const void *b, size_t c, int d, const void *e, unsigned f) { errno = ENOSYS; return -1; }
API(i64, sendmsg, int a, const void *b, int c) { errno = ENOSYS; return -1; }
API(int, getaddrinfo, const char *a, const char *b, const void *c, void **d) { return -2; }      /* EAI_NONAME */
API(void, freeaddrinfo, void *a) {}
API(void, freeifaddrs, void *a) {}
API(int, ioctl, int fd, unsigned long req, ...) { (void)fd; (void)req; errno = ENOSYS; return -1; }

/* ---- raw Linux system calls: the engine's start-up unpacker (and maybe other code) runs the `syscall` instruction itself with
   Linux numbers. macOS answers a number it does not know with SIGSYS, delivered right after the instruction. The handler runs the
   macOS equivalent, puts the result in rax the Linux way (-errno on failure) and lets the code continue. Unknown numbers are
   printed (once each) and answered -ENOSYS. ------------------------------------------------------------------------------ */
#include <signal.h>
#include <sys/ucontext.h>
static int lx_err(void) { int e = errno; return e == EAGAIN ? 11 : e == ETIMEDOUT ? 110 : e == ENOSYS ? 38 : e; }
static unsigned char g_unknown_seen[512];
static i64 linux_syscall(i64 nr, i64 a, i64 b, i64 c, i64 d, i64 e, i64 f) {
    i64 r;
    switch (nr) {
    case 0: r = read((int)a, (void *)b, (size_t)c); break;
    case 1: r = write((int)a, (const void *)b, (size_t)c); break;
    case 2: r = open((const char *)a, mac_oflags((int)b), (int)c); break;
    case 3: r = close((int)a); break;
    case 8: r = lseek((int)a, b, (int)c); break;
    case 9: { void *p = a_mmap((void *)a, (size_t)b, (int)c, (int)d, (int)e, f); r = p == MAP_FAILED ? -1 : (i64)p; break; }
    case 10: r = mprotect((void *)a, (size_t)b, (int)c); break;
    case 11: r = munmap((void *)a, (size_t)b); break;
    case 17: r = pread((int)a, (void *)b, (size_t)c, d); break;
    case 28: r = 0; break;                                                          /* madvise */
    case 39: r = getpid(); break;
    case 96: r = gettimeofday((struct timeval *)a, NULL); break;
    case 102: case 104: case 107: case 108: r = 10000; break;                       /* getuid / getgid / geteuid / getegid */
    case 157: r = 0; break;                                                         /* prctl: accepted, nothing to do */
    case 186: r = a_gettid(); break;
    case 201: r = time(NULL); if (a) *(i64 *)a = r; break;
    case 202: r = a_syscall(202, a, b, c, d, e, f); break;                          /* futex */
    case 228: r = a_clock_gettime((int)a, (struct timespec *)b); break;
    case 257: r = openat(mac_dirfd((int)a), (const char *)b, mac_oflags((int)c), (int)d); break;
    case 318: arc4random_buf((void *)a, (size_t)b); r = b; break;                   /* getrandom */
    default:
        if (nr < 0 || nr >= 512 || !g_unknown_seen[nr]) {
            if (nr >= 0 && nr < 512) g_unknown_seen[nr] = 1;
            fprintf(stderr, "crx: raw Linux syscall %lld (%llx, %llx, %llx) not emulated: -ENOSYS\n", nr, a, b, c); fflush(stderr);
        }
        return -38;
    }
    if (r < 0 && nr != 9) return -lx_err();
    bio_log("crx: raw syscall %lld (%llx, %llx, %llx) -> %llx\n", nr, a, b, c, r);
    return r;
}
static void on_sigsys(int sig, siginfo_t *info, void *ctx) {
    (void)sig; (void)info;
    _STRUCT_X86_THREAD_STATE64 *s = &((ucontext_t *)ctx)->uc_mcontext->__ss;
    s->__rax = (u64)linux_syscall((i64)s->__rax, (i64)s->__rdi, (i64)s->__rsi, (i64)s->__rdx, (i64)s->__r10, (i64)s->__r8, (i64)s->__r9);
}
/* Timer precision for the whole process: a process without a window gets "background" timer slack (App Nap), which stretched
   the engine's 16 ms frame sleep to ~20 ms. Latency QoS tier 0 = the tightest timers (what a foreground app gets). */
#include <mach/mach.h>
#include <mach/task_policy.h>
__attribute__((constructor)) static void precise_timers(void) {
    struct task_qos_policy q = {LATENCY_QOS_TIER_0, THROUGHPUT_QOS_TIER_0};
    kern_return_t k = task_policy_set(mach_task_self(), TASK_OVERRIDE_QOS_POLICY, (task_policy_t)&q, TASK_QOS_POLICY_COUNT);
    if (k != KERN_SUCCESS) k = task_policy_set(mach_task_self(), TASK_BASE_QOS_POLICY, (task_policy_t)&q, TASK_QOS_POLICY_COUNT);
    bio_log("crx: task latency QoS tier 0: %d\n", k);
}
__attribute__((constructor)) static void install_sigsys(void) {
    struct sigaction sa; memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = on_sigsys; sa.sa_flags = SA_SIGINFO; sigemptyset(&sa.sa_mask);
    sigaction(SIGSYS, &sa, NULL);
}

/* nanosleep: the engine's frame limiter sleeps ~16 ms a frame (60 fps). macOS adds slack to the timers of a process without a
   window (timer coalescing; even a plain native program asked for 16 ms sleeps 19-20): the game ran at ~50 fps (found
   2026-10-13; neither user-interactive QoS nor latency tier 0 helped). A thread that sleeps a frame's length here becomes a
   time-constraint (real-time) thread, as audio threads are: its sleeps then end on time (16.03 ms for 16 in a test). Period
   16.7 ms, up to 8 ms of work, all within 16 ms.  CRX_LOG_SLEEP=1 prints each request and how long it really slept. */
#include <mach/thread_policy.h>
#include <mach/mach_time.h>
static __thread int t_rt_set;
static void make_realtime(void) {
    mach_timebase_info_data_t tb; mach_timebase_info(&tb);
    double per_ms = (double)tb.denom / tb.numer * 1e6;
    thread_time_constraint_policy_data_t p = {(uint32_t)(16.67 * per_ms), (uint32_t)(8 * per_ms), (uint32_t)(16 * per_ms), 1};
    kern_return_t k = thread_policy_set(mach_thread_self(), THREAD_TIME_CONSTRAINT_POLICY, (thread_policy_t)&p,
                                        THREAD_TIME_CONSTRAINT_POLICY_COUNT);
    bio_log("crx: thread %d real-time for its frame sleeps: %d\n", a_gettid(), k);
}
API(int, nanosleep, const struct timespec *req, struct timespec *rem) {
    if (!t_rt_set && req->tv_sec == 0 && req->tv_nsec > 2000000 && req->tv_nsec < 40000000 && !getenv("CRX_NO_REALTIME")) {
        t_rt_set = 1; make_realtime();
    }
    static int log = -1; if (log < 0) log = getenv("CRX_LOG_SLEEP") != NULL;
    if (log) { struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0); int r = nanosleep(req, rem); clock_gettime(CLOCK_MONOTONIC, &t1);
        fprintf(stderr, "crx: nanosleep asked %.3f ms, slept %.3f ms (thread %d)\n", req->tv_sec * 1e3 + req->tv_nsec / 1e6,
                (t1.tv_sec - t0.tv_sec) * 1e3 + (t1.tv_nsec - t0.tv_nsec) / 1e6, a_gettid()); return r; }
    return nanosleep(req, rem);
}
