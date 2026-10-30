/* Test-only interposition of the installed SQLite Unix VFS sync calls.
 * Markers surround the real syscall; neither marker claims a kill inside it.
 * Arm from the actual commit thread, with one exact WAL identity. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static __thread struct {
    int active, ordinal, wal_ordinal, pause_ordinal, inject_directory;
    char operation[257], case_id[257], database[PATH_MAX], directory[PATH_MAX];
    char channel[128], role[32], pause_boundary[64];
} probe;

void p5_arm(const char *operation, const char *case_id, const char *database,
            const char *channel, const char *role, const char *pause_boundary,
            int pause_ordinal, int inject_directory) {
    memset(&probe, 0, sizeof(probe));
    snprintf(probe.operation, sizeof(probe.operation), "%s", operation);
    snprintf(probe.case_id, sizeof(probe.case_id), "%s", case_id);
    snprintf(probe.database, sizeof(probe.database), "%s", database);
    snprintf(probe.directory, sizeof(probe.directory), "%s", database);
    char *slash = strrchr(probe.directory, '/');
    if (slash) *slash = 0;
    snprintf(probe.channel, sizeof(probe.channel), "%s", channel);
    snprintf(probe.role, sizeof(probe.role), "%s", role);
    snprintf(probe.pause_boundary, sizeof(probe.pause_boundary), "%s", pause_boundary);
    probe.pause_ordinal = pause_ordinal;
    probe.inject_directory = inject_directory;
    probe.active = 1;
}

void p5_disarm(void) { probe.active = 0; }

static void escape_json(const char *in, char *out, size_t capacity) {
    size_t i = 0;
    for (; *in && i + 7 < capacity; ++in) {
        unsigned char c = (unsigned char)*in;
        if (c == '"' || c == '\\') { out[i++] = '\\'; out[i++] = c; }
        else if (c < 32) { i += (size_t)snprintf(out+i, capacity-i, "\\u%04x", c); }
        else out[i++] = c;
    }
    out[i] = 0;
}

static void mark(const char *boundary, const char *call, int fd,
                 const char *path, int result, int error, int wal) {
    const char *marker = getenv("P5_NATIVE_MARKER_FD");
    if (!marker) return;
    char line[8192], escaped[PATH_MAX*2];
    escape_json(path, escaped, sizeof(escaped));
    struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
    struct stat identity; memset(&identity, 0, sizeof(identity)); fstat(fd, &identity);
    int n = snprintf(line, sizeof(line),
        "{\"boundary\":\"%s\",\"operation_id\":\"%s\",\"case_id\":\"%s\","
        "\"pid\":%d,\"tid\":%ld,\"db_role\":\"%s\",\"channel\":\"%s\","
        "\"path\":\"%s\",\"fd\":%d,\"device\":%lu,\"inode\":%lu,"
        "\"monotonic_ns\":%lld,\"call_ordinal\":%d,\"wal_ordinal\":%d,"
        "\"syscall\":\"%s\",\"result\":%d,\"errno\":%d,\"path_kind\":\"%s\","
        "\"transaction_phase\":\"native_commit_active\"}\n",
        boundary, probe.operation, probe.case_id, getpid(), syscall(SYS_gettid),
        probe.role, probe.channel, escaped, fd, (unsigned long)identity.st_dev,
        (unsigned long)identity.st_ino, (long long)now.tv_sec*1000000000+now.tv_nsec,
        probe.ordinal, probe.wal_ordinal, call, result, error, wal ? "wal" : "directory");
    if (n > 0 && n < (int)sizeof(line)) {
        ssize_t ignored = write(atoi(marker), line, (size_t)n); (void)ignored;
    }
    if (wal && probe.wal_ordinal == probe.pause_ordinal
        && strcmp(boundary, probe.pause_boundary) == 0) {
        const char *release = getenv("P5_NATIVE_RELEASE_FD");
        if (release) {
            char token;
            while (read(atoi(release), &token, 1) < 0 && errno == EINTR) {}
        }
    }
}

static int selected(int fd, char *path, int *wal) {
    if (!probe.active) return 0;
    char proc[64]; snprintf(proc, sizeof(proc), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(proc, path, PATH_MAX-1);
    if (n < 0) return 0;
    path[n] = 0;
    size_t length = strlen(probe.database);
    *wal = strncmp(path, probe.database, length) == 0 && strcmp(path+length, "-wal") == 0;
    return *wal || strcmp(path, probe.directory) == 0;
}

static int sync_call(int fd, const char *name, int (*real)(int)) {
    char path[PATH_MAX]; int wal;
    if (!selected(fd, path, &wal)) return real(fd);
    ++probe.ordinal;
    if (wal) ++probe.wal_ordinal;
    mark("sqlite_native_sync_enter", name, fd, path, -2, 0, wal);
    int result;
    if (!wal && probe.inject_directory) { errno = EIO; result = -1; }
    else result = real(fd);
    int error = result < 0 ? errno : 0;
    mark("sqlite_native_sync_return", name, fd, path, result, error, wal);
    errno = error;
    return result;
}

int fsync(int fd) {
    static int (*real)(int);
    if (!real) real = dlsym(RTLD_NEXT, "fsync");
    return sync_call(fd, "fsync", real);
}

int fdatasync(int fd) {
    static int (*real)(int);
    if (!real) real = dlsym(RTLD_NEXT, "fdatasync");
    return sync_call(fd, "fdatasync", real);
}
