/*
 * rxbench: native RandomX benchmark driver for rxlab.
 *
 * Initialises the cache/dataset once per process, then runs sweeps of hashing
 * configurations and prints one JSON object per line on stdout. Every hash is
 * computed by the public randomx.h API of the linked librandomx.
 *
 *   rxbench [--seed N] [--init-threads N] COMMAND
 *   COMMAND is one of
 *     info
 *     sweep  [--warmup S] [--measure S] [--trials N] [--order interleave|block] CONFIG...
 *     sample [--interval S] [--duration S] CONFIG
 *     init   [--threads-list 1,2,..] [--trials N] [--fraction F] [--qos CLASS]
 *     verify [--nonces N] CONFIG
 *     serve  (reads one COMMAND per stdin line, so one dataset serves them all)
 * Every command's output ends with {"type":"end"}.
 *
 * CONFIG is key=value pairs joined by commas, e.g.
 *   v=1,mode=fast,jit=1,aes=hard,batch=1,threads=4,qos=ui*4+bg*6
 * qos entries are class*count joined by '+'; the last class fills any
 * remaining threads. Classes: unset, user_interactive (ui), user_initiated (in),
 * default (def), utility (ut), background (bg).
 *
 * macOS has no hard thread affinity: QoS is a scheduling hint (background QoS
 * is kept on E-cores; higher classes prefer P-cores but may spill). Where a
 * thread actually ran is sampled with pthread_cpu_number_np after every hash.
 *
 * Input blob, nonce offset, seed encoding and the verify subcommand follow the
 * upstream randomx-benchmark, so `verify --nonces 1000` with seed 0 must print
 * the upstream reference result.
 */
#define _DARWIN_C_SOURCE
#define _GNU_SOURCE
#include <inttypes.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach/mach.h>
#include <objc/message.h>
#include <objc/runtime.h>
#include <pthread/qos.h>
#elif defined(__linux__)
#include <sched.h>
#endif
#include "randomx.h"

#ifndef RXBENCH_TAG
#define RXBENCH_TAG "unknown"
#endif
#ifndef RXBENCH_V2
#define RXBENCH_V2 0
#endif

#define FLAG_V2 128
#define MAX_THREADS 64
#define MAX_CPUS 64
#define NONCE_OFFSET 39
#define NONCE_STRIDE (1u << 24)
#define CHECK_NONCE 0u
#define MAX_EXTEND 10.0

static const uint8_t TEMPLATE[76] = {
    0x07, 0x07, 0xf7, 0xa4, 0xf0, 0xd6, 0x05, 0xb3, 0x03, 0x26, 0x08, 0x16, 0xba, 0x3f, 0x10, 0x90, 0x2e, 0x1a, 0x14,
    0x5a, 0xc5, 0xfa, 0xd3, 0xaa, 0x3a, 0xf6, 0xea, 0x44, 0xc1, 0x18, 0x69, 0xdc, 0x4f, 0x85, 0x3f, 0x00, 0x2b, 0x2e,
    0xea, 0x00, 0x00, 0x00, 0x00, 0x77, 0xb2, 0x06, 0xa0, 0x2c, 0xa5, 0xb1, 0xd4, 0xce, 0x6b, 0xbf, 0xdf, 0x0a, 0xca,
    0xc3, 0x8b, 0xde, 0xd3, 0x4d, 0x2d, 0xcd, 0xee, 0xf9, 0x5c, 0xd2, 0x0c, 0xef, 0xc1, 0x2f, 0x61, 0xd5, 0x61, 0x09,
};

enum { PH_READY, PH_WARMUP, PH_MEASURE, PH_STOP };

static const struct {
    const char *name, *alias;
    int cls;
} QOS[] = {
#ifdef __APPLE__
    {"unset", "unset", -1},
    {"user_interactive", "ui", QOS_CLASS_USER_INTERACTIVE},
    {"user_initiated", "in", QOS_CLASS_USER_INITIATED},
    {"default", "def", QOS_CLASS_DEFAULT},
    {"utility", "ut", QOS_CLASS_UTILITY},
    {"background", "bg", QOS_CLASS_BACKGROUND},
#else
    {"unset", "unset", -1},
#endif
};
#define NQOS ((int)(sizeof QOS / sizeof QOS[0]))

typedef struct {
    int v2, full, jit, hard_aes, batch, threads;
    int qos[MAX_THREADS];
    char qos_spec[160];
    char spec[256];
} config_t;

typedef struct {
    _Alignas(128) _Atomic uint64_t total;
    const config_t *cfg;
    randomx_flags flags;
    int index, vm_ok, qos_got;
    pthread_t tid;
    uint64_t n;
    double t_first, t_last;
    int started;
    uint32_t first_nonce, last_nonce;
    uint8_t first_hash[RANDOMX_HASH_SIZE], last_hash[RANDOMX_HASH_SIZE], check[RANDOMX_HASH_SIZE];
    uint64_t cpu_hist[MAX_CPUS];
} worker_t;

static struct {
    uint8_t seed[4];
    int32_t seed_value;
    int init_threads;
    randomx_flags lib_flags;
    randomx_cache *cache[2];
    randomx_dataset *dataset;
    int dataset_valid;
} G;

static _Atomic int phase;
static _Atomic int ready;
static _Atomic uint64_t cpu_total[MAX_CPUS];

static void die(const char *msg, const char *arg) {
    fprintf(stderr, "rxbench: %s%s%s\n", msg, arg ? ": " : "", arg ? arg : "");
    exit(2);
}

static double now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + 1e-9 * (double)ts.tv_nsec;
}

static void sleep_until(double deadline) {
    for (double left; (left = deadline - now()) > 0;) {
        struct timespec ts = {(time_t)left, (long)((left - (double)(time_t)left) * 1e9)};
        nanosleep(&ts, NULL);
    }
}

static int cpu_now(void) {
#ifdef __APPLE__
    size_t c;
    return pthread_cpu_number_np(&c) == 0 ? (int)c : -1;
#elif defined(__linux__)
    return sched_getcpu();
#else
    return -1;
#endif
}

/* NSProcessInfo.thermalState: 0 nominal, 1 fair, 2 serious, 3 critical; -1 unknown. */
static int thermal_state(void) {
#ifdef __APPLE__
    Class cls = objc_getClass("NSProcessInfo");
    if (!cls)
        return -1;
    id info = ((id (*)(Class, SEL))objc_msgSend)(cls, sel_registerName("processInfo"));
    return info ? (int)((long (*)(id, SEL))objc_msgSend)(info, sel_registerName("thermalState")) : -1;
#else
    return -1;
#endif
}

/* Process page faults plus system-wide compressor activity: a trial whose window
 * shows decompressions or swap-ins was slowed by memory pressure. */
typedef struct {
    uint64_t minflt, majflt, decompressions, swapins;
} vmstat_t;

static vmstat_t vm_now(void) {
    vmstat_t v = {0, 0, 0, 0};
    struct rusage ru;
    if (getrusage(RUSAGE_SELF, &ru) == 0) {
        v.minflt = (uint64_t)ru.ru_minflt;
        v.majflt = (uint64_t)ru.ru_majflt;
    }
#ifdef __APPLE__
    vm_statistics64_data_t st;
    mach_msg_type_number_t n = HOST_VM_INFO64_COUNT;
    if (host_statistics64(mach_host_self(), HOST_VM_INFO64, (host_info64_t)&st, &n) == KERN_SUCCESS) {
        v.decompressions = st.decompressions;
        v.swapins = st.swapins;
    }
#endif
    return v;
}

static void print_vm(vmstat_t a, vmstat_t b) {
    printf(",\"vm\":{\"minflt\":%" PRIu64 ",\"majflt\":%" PRIu64 ",\"decompressions\":%" PRIu64
           ",\"swapins\":%" PRIu64 "}",
           b.minflt - a.minflt, b.majflt - a.majflt, b.decompressions - a.decompressions, b.swapins - a.swapins);
}

static void set_qos(int q) {
#ifdef __APPLE__
    if (QOS[q].cls >= 0)
        pthread_set_qos_class_self_np((qos_class_t)QOS[q].cls, 0);
#else
    (void)q;
#endif
}

static int qos_self(void) {
#ifdef __APPLE__
    int cls = (int)qos_class_self();
    for (int i = 1; i < NQOS; i++)
        if (QOS[i].cls == cls)
            return i;
#endif
    return 0;
}

static void store32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static void hex(char *out, const uint8_t *in, size_t n) {
    static const char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[2 * i] = digits[in[i] >> 4];
        out[2 * i + 1] = digits[in[i] & 15];
    }
    out[2 * n] = 0;
}

static int qos_lookup(const char *s, size_t len) {
    for (int i = 0; i < NQOS; i++)
        if ((strlen(QOS[i].name) == len && !strncmp(s, QOS[i].name, len)) ||
            (strlen(QOS[i].alias) == len && !strncmp(s, QOS[i].alias, len)))
            return i;
    return -1;
}

static void parse_qos(config_t *c, const char *spec) {
    int filled = 0, last = 0;
    char buf[160];
    snprintf(buf, sizeof buf, "%s", spec);
    c->qos_spec[0] = 0;
    for (char *save = NULL, *item = strtok_r(buf, "+", &save); item; item = strtok_r(NULL, "+", &save)) {
        char *star = strchr(item, '*');
        int count = star ? atoi(star + 1) : -1;
        int q = qos_lookup(item, star ? (size_t)(star - item) : strlen(item));
        if (q < 0 || (star && count <= 0))
            die("bad qos entry", item);
        if (count < 0)
            count = MAX_THREADS;
        for (int i = 0; i < count && filled < MAX_THREADS; i++)
            c->qos[filled++] = q;
        size_t used = strlen(c->qos_spec);
        snprintf(c->qos_spec + used, sizeof c->qos_spec - used, "%s%s", used ? "+" : "", QOS[q].alias);
        if (star) {
            used = strlen(c->qos_spec);
            snprintf(c->qos_spec + used, sizeof c->qos_spec - used, "*%d", count);
        }
        last = q;
    }
    while (filled < MAX_THREADS)
        c->qos[filled++] = last;
}

static config_t parse_config(const char *spec) {
    config_t c = {.v2 = 0, .full = 1, .jit = 1, .hard_aes = 1, .batch = 1, .threads = 1};
    parse_qos(&c, "unset");
    char buf[256];
    snprintf(buf, sizeof buf, "%s", spec);
    for (char *save = NULL, *kv = strtok_r(buf, ",", &save); kv; kv = strtok_r(NULL, ",", &save)) {
        char *eq = strchr(kv, '=');
        if (!eq)
            die("config item needs key=value", kv);
        *eq = 0;
        const char *k = kv, *v = eq + 1;
        if (!strcmp(k, "v"))
            c.v2 = !strcmp(v, "2") ? 1 : !strcmp(v, "1") ? 0 : (die("v must be 1 or 2", v), 0);
        else if (!strcmp(k, "mode"))
            c.full = !strcmp(v, "fast") ? 1 : !strcmp(v, "light") ? 0 : (die("mode must be fast or light", v), 0);
        else if (!strcmp(k, "jit"))
            c.jit = atoi(v) != 0;
        else if (!strcmp(k, "aes"))
            c.hard_aes = !strcmp(v, "hard") ? 1 : !strcmp(v, "soft") ? 0 : (die("aes must be hard or soft", v), 0);
        else if (!strcmp(k, "batch"))
            c.batch = atoi(v) != 0;
        else if (!strcmp(k, "threads"))
            c.threads = atoi(v);
        else if (!strcmp(k, "qos"))
            parse_qos(&c, v);
        else
            die("unknown config key", k);
    }
    if (c.threads < 1 || c.threads > MAX_THREADS)
        die("threads must be in 1..64", spec);
    if (c.v2 && !RXBENCH_V2)
        die("v=2 needs a RandomX v2.x library; this binary is linked to", RXBENCH_TAG);
    snprintf(c.spec, sizeof c.spec, "v=%d,mode=%s,jit=%d,aes=%s,batch=%d,threads=%d,qos=%s", c.v2 ? 2 : 1,
             c.full ? "fast" : "light", c.jit, c.hard_aes ? "hard" : "soft", c.batch, c.threads, c.qos_spec);
    return c;
}

static randomx_flags cache_flags(int jit) {
    int f = G.lib_flags & RANDOMX_FLAG_ARGON2;
    if (jit)
        f |= RANDOMX_FLAG_JIT | (G.lib_flags & RANDOMX_FLAG_SECURE);
    return (randomx_flags)f;
}

static randomx_flags vm_flags(const config_t *c) {
    int f = cache_flags(c->jit);
    if (c->hard_aes)
        f |= RANDOMX_FLAG_HARD_AES;
    if (c->full)
        f |= RANDOMX_FLAG_FULL_MEM;
    if (c->v2)
        f |= FLAG_V2;
    return (randomx_flags)f;
}

static randomx_cache *get_cache(int jit) {
    jit = jit && (G.lib_flags & RANDOMX_FLAG_JIT);
    if (!G.cache[jit]) {
        double t = now();
        if (!(G.cache[jit] = randomx_alloc_cache(cache_flags(jit))))
            die("randomx_alloc_cache failed", NULL);
        randomx_init_cache(G.cache[jit], G.seed, sizeof G.seed);
        printf("{\"type\":\"cache\",\"jit\":%d,\"seconds\":%.6f}\n", jit, now() - t);
        fflush(stdout);
    }
    return G.cache[jit];
}

typedef struct {
    randomx_dataset *ds;
    randomx_cache *cache;
    unsigned long start, count;
    int qos;
} init_job_t;

static void *init_worker(void *arg) {
    init_job_t *j = arg;
    set_qos(j->qos);
    randomx_init_dataset(j->ds, j->cache, j->start, j->count);
    return NULL;
}

/* Initialise items [0, items) with `threads` threads; returns elapsed seconds. */
static double init_dataset(randomx_dataset *ds, unsigned long items, int threads, int qos) {
    init_job_t jobs[MAX_THREADS];
    pthread_t tids[MAX_THREADS];
    randomx_cache *cache = get_cache(1);
    double t = now();
    for (int i = 0; i < threads; i++) {
        unsigned long lo = items * (unsigned long)i / (unsigned long)threads;
        unsigned long hi = items * (unsigned long)(i + 1) / (unsigned long)threads;
        jobs[i] = (init_job_t){ds, cache, lo, hi - lo, qos};
        pthread_create(&tids[i], NULL, init_worker, &jobs[i]);
    }
    for (int i = 0; i < threads; i++)
        pthread_join(tids[i], NULL);
    return now() - t;
}

static randomx_dataset *alloc_dataset(void) {
    if (!G.dataset && !(G.dataset = randomx_alloc_dataset(cache_flags(1))))
        die("randomx_alloc_dataset failed", NULL);
    return G.dataset;
}

static randomx_dataset *get_dataset(void) {
    if (!G.dataset_valid) {
        double s = init_dataset(alloc_dataset(), randomx_dataset_item_count(), G.init_threads, 0);
        G.dataset_valid = 1;
        printf("{\"type\":\"dataset\",\"threads\":%d,\"seconds\":%.6f}\n", G.init_threads, s);
        fflush(stdout);
    }
    return G.dataset;
}

static void record(worker_t *w, uint32_t nonce, const uint8_t *out) {
    int cpu = cpu_now();
    atomic_fetch_add_explicit(&w->total, 1, memory_order_relaxed);
    if (cpu >= 0 && cpu < MAX_CPUS)
        atomic_fetch_add_explicit(&cpu_total[cpu], 1, memory_order_relaxed);
    if (atomic_load_explicit(&phase, memory_order_acquire) != PH_MEASURE)
        return;
    double t = now();
    if (!w->started) {
        w->started = 1;
        w->t_first = t;
        w->first_nonce = nonce;
        memcpy(w->first_hash, out, RANDOMX_HASH_SIZE);
    } else {
        w->n++;
        if (cpu >= 0 && cpu < MAX_CPUS)
            w->cpu_hist[cpu]++;
    }
    w->t_last = t;
    w->last_nonce = nonce;
    memcpy(w->last_hash, out, RANDOMX_HASH_SIZE);
}

static void *hash_worker(void *arg) {
    worker_t *w = arg;
    const config_t *c = w->cfg;
    set_qos(c->qos[w->index]);
    w->qos_got = qos_self();
    randomx_vm *vm = randomx_create_vm(w->flags, c->full ? NULL : get_cache(c->jit), c->full ? G.dataset : NULL);
    w->vm_ok = vm != NULL;
    uint8_t in[sizeof TEMPLATE], out[RANDOMX_HASH_SIZE];
    memcpy(in, TEMPLATE, sizeof in);
    if (vm && w->index == 0) {
        store32(in + NONCE_OFFSET, CHECK_NONCE);
        randomx_calculate_hash(vm, in, sizeof in, w->check);
    }
    atomic_fetch_add(&ready, 1);
    while (atomic_load(&phase) == PH_READY)
        usleep(200);
    if (!vm)
        return NULL;
    uint32_t nonce = (uint32_t)w->index * NONCE_STRIDE, pending = nonce;
    if (c->batch) {
        store32(in + NONCE_OFFSET, nonce++);
        randomx_calculate_hash_first(vm, in, sizeof in);
    }
    while (atomic_load_explicit(&phase, memory_order_acquire) != PH_STOP) {
        store32(in + NONCE_OFFSET, nonce);
        if (c->batch) {
            randomx_calculate_hash_next(vm, in, sizeof in, out);
            record(w, pending, out);
            pending = nonce++;
        } else {
            randomx_calculate_hash(vm, in, sizeof in, out);
            record(w, nonce++, out);
        }
    }
    if (c->batch)
        randomx_calculate_hash_last(vm, out);
    randomx_destroy_vm(vm);
    return NULL;
}

static worker_t *start_workers(const config_t *c) {
    worker_t *ws = aligned_alloc(128, sizeof(worker_t) * (size_t)c->threads);
    if (!ws)
        die("out of memory", NULL);
    memset(ws, 0, sizeof(worker_t) * (size_t)c->threads);
    if (c->full)
        get_dataset();
    else
        get_cache(c->jit);
    atomic_store(&phase, PH_READY);
    atomic_store(&ready, 0);
    for (int i = 0; i < MAX_CPUS; i++)
        atomic_store(&cpu_total[i], 0);
    for (int i = 0; i < c->threads; i++) {
        ws[i].cfg = c;
        ws[i].flags = vm_flags(c);
        ws[i].index = i;
        pthread_create(&ws[i].tid, NULL, hash_worker, &ws[i]);
    }
    while (atomic_load(&ready) < c->threads)
        usleep(500);
    return ws;
}

static void stop_workers(const config_t *c, worker_t *ws) {
    atomic_store(&phase, PH_STOP);
    for (int i = 0; i < c->threads; i++)
        pthread_join(ws[i].tid, NULL);
}

static void print_config(const config_t *c) {
    printf("\"config\":\"%s\",\"v\":%d,\"mode\":\"%s\",\"jit\":%d,\"aes\":\"%s\",\"batch\":%d,\"threads\":%d,"
           "\"qos\":\"%s\",\"flags\":%d",
           c->spec, c->v2 ? 2 : 1, c->full ? "fast" : "light", c->jit, c->hard_aes ? "hard" : "soft", c->batch,
           c->threads, c->qos_spec, (int)vm_flags(c));
}

/* Per-thread detail shared by sweep trials and sample runs; returns summed per-thread rates. */
static double print_threads(const config_t *c, worker_t *ws, int *complete) {
    char h[2 * RANDOMX_HASH_SIZE + 1];
    double total = 0;
    *complete = 1;
    printf(",\"per_thread\":[");
    for (int i = 0; i < c->threads; i++) {
        worker_t *w = &ws[i];
        double span = w->t_last - w->t_first, hs = w->n && span > 0 ? (double)w->n / span : 0;
        *complete &= w->n > 0;
        total += hs;
        printf("%s{\"qos\":\"%s\",\"hashes\":%" PRIu64 ",\"span_s\":%.6f,\"hs\":%.4f", i ? "," : "",
               QOS[w->qos_got].name, w->n, span, hs);
        if (w->started) {
            hex(h, w->first_hash, RANDOMX_HASH_SIZE);
            printf(",\"first\":[%" PRIu32 ",\"%s\"]", w->first_nonce, h);
            hex(h, w->last_hash, RANDOMX_HASH_SIZE);
            printf(",\"last\":[%" PRIu32 ",\"%s\"]", w->last_nonce, h);
        }
        printf(",\"cpus\":{");
        for (int k = 0, sep = 0; k < MAX_CPUS; k++)
            if (w->cpu_hist[k])
                printf("%s\"%d\":%" PRIu64, sep++ ? "," : "", k, w->cpu_hist[k]);
        printf("}}");
    }
    hex(h, ws[0].check, RANDOMX_HASH_SIZE);
    printf("],\"check\":[%u,\"%s\"]", CHECK_NONCE, h);
    return total;
}

static void run_trial(const config_t *c, int trial, double warmup, double measure) {
    worker_t *ws = start_workers(c);
    int vms_ok = 1;
    for (int i = 0; i < c->threads; i++)
        vms_ok &= ws[i].vm_ok;
    double t0 = 0, t1 = 0;
    uint64_t c0 = 0, c1 = 0;
    vmstat_t vm0 = {0, 0, 0, 0}, vm1 = vm0;
    int th0 = thermal_state();
    if (vms_ok) {
        atomic_store(&phase, PH_WARMUP);
        sleep_until(now() + warmup);
        uint64_t base[MAX_THREADS];
        for (int i = 0; i < c->threads; i++)
            c0 += base[i] = atomic_load(&ws[i].total);
        vm0 = vm_now();
        t0 = now();
        atomic_store(&phase, PH_MEASURE);
        sleep_until(t0 + measure);
        /* Slow configs: stretch the window (up to MAX_EXTEND x) until every thread
         * has at least one fully timed hash; 3 completions past `base` guarantee it. */
        for (int i = 0; i < c->threads && now() < t0 + MAX_EXTEND * measure;)
            if (atomic_load(&ws[i].total) - base[i] >= 3)
                i++;
            else
                usleep(1000);
        atomic_store(&phase, PH_WARMUP);
        t1 = now();
        vm1 = vm_now();
        for (int i = 0; i < c->threads; i++)
            c1 += atomic_load(&ws[i].total);
    }
    stop_workers(c, ws);
    printf("{\"type\":\"trial\",");
    print_config(c);
    if (!vms_ok) {
        printf(",\"trial\":%d,\"error\":\"randomx_create_vm failed\"}\n", trial);
    } else {
        int complete;
        printf(",\"trial\":%d,\"warmup_s\":%.3f,\"window_s\":%.6f", trial, warmup, t1 - t0);
        double hs = print_threads(c, ws, &complete);
        printf(",\"hs\":%.4f,\"hs_window\":%.4f,\"complete\":%s,\"thermal\":[%d,%d]", hs,
               (double)(c1 - c0) / (t1 - t0), complete ? "true" : "false", th0, thermal_state());
        print_vm(vm0, vm1);
        printf("}\n");
    }
    fflush(stdout);
    free(ws);
}

static void cmd_sweep(config_t *cs, int n, double warmup, double measure, int trials, int interleave) {
    for (int a = 0; a < (interleave ? trials : n); a++)
        for (int b = 0; b < (interleave ? n : trials); b++)
            run_trial(&cs[interleave ? b : a], interleave ? a : b, warmup, measure);
}

static void cmd_sample(const config_t *c, double interval, double duration) {
    worker_t *ws = start_workers(c);
    uint64_t prev[MAX_THREADS] = {0}, prev_cpu[MAX_CPUS] = {0};
    for (int i = 0; i < c->threads; i++)
        if (!ws[i].vm_ok)
            die("randomx_create_vm failed", c->spec);
    double t0 = now(), tprev = t0;
    vmstat_t vprev = vm_now();
    atomic_store(&phase, PH_MEASURE);
    for (int k = 1; tprev - t0 < duration - 1e-9; k++) {
        double target = t0 + k * interval;
        sleep_until(target < t0 + duration ? target : t0 + duration);
        double t = now(), rate = 0;
        printf("{\"type\":\"sample\",\"t\":%.6f,\"dt\":%.6f,\"hashes\":[", t - t0, t - tprev);
        for (int i = 0; i < c->threads; i++) {
            uint64_t cur = atomic_load(&ws[i].total);
            printf("%s%" PRIu64, i ? "," : "", cur - prev[i]);
            rate += (double)(cur - prev[i]);
            prev[i] = cur;
        }
        printf("],\"hs\":%.4f,\"cpus\":{", rate / (t - tprev));
        for (int j = 0, sep = 0; j < MAX_CPUS; j++) {
            uint64_t cur = atomic_load(&cpu_total[j]);
            if (cur - prev_cpu[j])
                printf("%s\"%d\":%" PRIu64, sep++ ? "," : "", j, cur - prev_cpu[j]);
            prev_cpu[j] = cur;
        }
        vmstat_t vcur = vm_now();
        printf("},\"thermal\":%d", thermal_state());
        print_vm(vprev, vcur);
        printf("}\n");
        fflush(stdout);
        tprev = t;
        vprev = vcur;
    }
    atomic_store(&phase, PH_WARMUP);
    double t_end = now();
    stop_workers(c, ws);
    int complete;
    printf("{\"type\":\"sample_end\",");
    print_config(c);
    printf(",\"duration_s\":%.6f", t_end - t0);
    double hs = print_threads(c, ws, &complete);
    printf(",\"hs\":%.4f,\"complete\":%s}\n", hs, complete ? "true" : "false");
    fflush(stdout);
    free(ws);
}

static uint64_t digest(const uint8_t *mem, size_t bytes) {
    uint64_t h = 0xcbf29ce484222325ull;
    const uint64_t *p = (const uint64_t *)mem;
    for (size_t i = 0; i < bytes / 8; i++)
        h = (h ^ p[i]) * 0x100000001b3ull;
    return h;
}

static void cmd_init(const int *counts, int n, int trials, double fraction, int qos) {
    unsigned long total = randomx_dataset_item_count();
    unsigned long items = (unsigned long)((double)total * fraction);
    if (items < 1 || items > total)
        die("fraction must be in (0, 1]", NULL);
    randomx_dataset *ds = alloc_dataset();
    uint8_t *mem = randomx_get_dataset_memory(ds);
    size_t bytes = (size_t)items * RANDOMX_DATASET_ITEM_SIZE;
    get_cache(1);
    for (int t = 0; t < trials; t++)
        for (int i = 0; i < n; i++) {
            memset(mem, 0, bytes);
            G.dataset_valid = 0;
            int th0 = thermal_state();
            double s = init_dataset(ds, items, counts[i], qos);
            printf("{\"type\":\"init\",\"threads\":%d,\"trial\":%d,\"qos\":\"%s\",\"items\":%lu,\"total_items\":%lu,"
                   "\"seconds\":%.6f,\"items_per_s\":%.1f,\"digest\":\"%016" PRIx64 "\",\"thermal\":[%d,%d]",
                   counts[i], t, QOS[qos].name, items, total, s, (double)items / s, digest(mem, bytes), th0,
                   thermal_state());
            if (items == total) {
                /* a fast-mode hash only matches light mode if the whole dataset is right */
                G.dataset_valid = 1;
                config_t c = parse_config("mode=fast,jit=1,aes=hard,batch=0,threads=1");
                randomx_vm *vm = randomx_create_vm(vm_flags(&c), NULL, ds);
                uint8_t in[sizeof TEMPLATE], out[RANDOMX_HASH_SIZE];
                char h[2 * RANDOMX_HASH_SIZE + 1];
                memcpy(in, TEMPLATE, sizeof in);
                store32(in + NONCE_OFFSET, CHECK_NONCE);
                if (!vm)
                    die("randomx_create_vm failed", c.spec);
                randomx_calculate_hash(vm, in, sizeof in, out);
                randomx_destroy_vm(vm);
                hex(h, out, RANDOMX_HASH_SIZE);
                printf(",\"check\":[%u,\"%s\"]", CHECK_NONCE, h);
            }
            printf("}\n");
            fflush(stdout);
        }
}

typedef struct {
    randomx_vm *vm;
    const config_t *cfg;
    _Atomic uint32_t *next;
    uint32_t count;
    uint64_t acc[4];
} verify_job_t;

static void *verify_worker(void *arg) {
    verify_job_t *j = arg;
    uint8_t in[sizeof TEMPLATE];
    uint64_t out[4];
    memcpy(in, TEMPLATE, sizeof in);
    uint32_t nonce = atomic_fetch_add(j->next, 1);
    if (j->cfg->batch && nonce < j->count) {
        store32(in + NONCE_OFFSET, nonce);
        randomx_calculate_hash_first(j->vm, in, sizeof in);
    }
    while (nonce < j->count) {
        if (j->cfg->batch) {
            nonce = atomic_fetch_add(j->next, 1);
            store32(in + NONCE_OFFSET, nonce);
            if (nonce < j->count)
                randomx_calculate_hash_next(j->vm, in, sizeof in, out);
            else
                randomx_calculate_hash_last(j->vm, out);
        } else {
            store32(in + NONCE_OFFSET, nonce);
            randomx_calculate_hash(j->vm, in, sizeof in, out);
            nonce = atomic_fetch_add(j->next, 1);
        }
        for (int k = 0; k < 4; k++)
            j->acc[k] ^= out[k];
    }
    return NULL;
}

/* XOR of the hashes of nonces [0, count), as printed by the upstream randomx-benchmark. */
static void cmd_verify(const config_t *c, uint32_t count) {
    verify_job_t jobs[MAX_THREADS];
    pthread_t tids[MAX_THREADS];
    _Atomic uint32_t next = 0;
    if (c->full)
        get_dataset();
    double t = now();
    for (int i = 0; i < c->threads; i++) {
        randomx_vm *vm = randomx_create_vm(vm_flags(c), c->full ? NULL : get_cache(c->jit), c->full ? G.dataset : NULL);
        if (!vm)
            die("randomx_create_vm failed", c->spec);
        jobs[i] = (verify_job_t){vm, c, &next, count, {0, 0, 0, 0}};
    }
    for (int i = 0; i < c->threads; i++)
        pthread_create(&tids[i], NULL, verify_worker, &jobs[i]);
    uint64_t acc[4] = {0, 0, 0, 0};
    for (int i = 0; i < c->threads; i++) {
        pthread_join(tids[i], NULL);
        randomx_destroy_vm(jobs[i].vm);
        for (int k = 0; k < 4; k++)
            acc[k] ^= jobs[i].acc[k];
    }
    char h[2 * RANDOMX_HASH_SIZE + 1];
    hex(h, (const uint8_t *)acc, RANDOMX_HASH_SIZE);
    printf("{\"type\":\"verify\",");
    print_config(c);
    printf(",\"seed\":%d,\"nonces\":%u,\"result\":\"%s\",\"seconds\":%.6f}\n", G.seed_value, count, h, now() - t);
}

static void cmd_info(void) {
    char tmpl[2 * sizeof TEMPLATE + 1];
    hex(tmpl, TEMPLATE, sizeof TEMPLATE);
    printf("{\"type\":\"info\",\"tag\":\"%s\",\"v2\":%s,\"lib_flags\":%d,\"dataset_items\":%lu,\"template\":\"%s\","
           "\"nonce_offset\":%d,\"nonce_stride\":%u,\"check_nonce\":%u,\"logical_cpus\":%ld,\"thermal\":%d,"
           "\"qos_classes\":[",
           RXBENCH_TAG, RXBENCH_V2 ? "true" : "false", (int)G.lib_flags, randomx_dataset_item_count(), tmpl,
           NONCE_OFFSET, NONCE_STRIDE, CHECK_NONCE, sysconf(_SC_NPROCESSORS_ONLN), thermal_state());
    for (int i = 0; i < NQOS; i++)
        printf("%s\"%s\"", i ? "," : "", QOS[i].name);
    printf("]}\n");
}

static int parse_list(const char *s, int *out, int max) {
    int n = 0;
    char buf[256];
    snprintf(buf, sizeof buf, "%s", s);
    for (char *save = NULL, *t = strtok_r(buf, ",", &save); t && n < max; t = strtok_r(NULL, ",", &save))
        if ((out[n++] = atoi(t)) < 1 || out[n - 1] > MAX_THREADS)
            die("thread counts must be in 1..64", t);
    return n;
}

static void command(int argc, char **argv, int top) {
    const char *cmd = argv[0];
    double warmup = 2, measure = 5, interval = 10, duration = 60, fraction = 1;
    int trials = 3, interleave = 1, counts[MAX_THREADS], ncounts = 0, init_qos = 0, nconf = 0;
    uint32_t nonces = 1000;
    config_t *cs = calloc((size_t)argc + 1, sizeof *cs);
    if (!cs)
        die("out of memory", NULL);
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i], *v = i + 1 < argc ? argv[i + 1] : NULL;
        if (strncmp(a, "--", 2)) {
            cs[nconf++] = parse_config(a);
            continue;
        }
        if (!v)
            die("missing value for", a);
        i++;
        if (!strcmp(a, "--warmup"))
            warmup = atof(v);
        else if (!strcmp(a, "--measure"))
            measure = atof(v);
        else if (!strcmp(a, "--trials"))
            trials = atoi(v);
        else if (!strcmp(a, "--order"))
            interleave = !strcmp(v, "interleave") ? 1 : !strcmp(v, "block") ? 0 : (die("bad --order", v), 0);
        else if (!strcmp(a, "--interval"))
            interval = atof(v);
        else if (!strcmp(a, "--duration"))
            duration = atof(v);
        else if (!strcmp(a, "--nonces"))
            nonces = (uint32_t)strtoul(v, NULL, 10);
        else if (!strcmp(a, "--threads-list"))
            ncounts = parse_list(v, counts, MAX_THREADS);
        else if (!strcmp(a, "--fraction"))
            fraction = atof(v);
        else if (!strcmp(a, "--qos")) {
            if ((init_qos = qos_lookup(v, strlen(v))) < 0)
                die("bad --qos", v);
        } else
            die("unknown option", a);
    }
    if (trials < 1 || measure <= 0 || warmup < 0 || interval <= 0 || duration <= 0)
        die("bad timing options", NULL);
    printf("{\"type\":\"start\",\"command\":\"%s\",\"tag\":\"%s\",\"seed\":%d,\"lib_flags\":%d}\n", cmd,
           RXBENCH_TAG, G.seed_value, (int)G.lib_flags);
    fflush(stdout);
    if (!strcmp(cmd, "info"))
        cmd_info();
    else if (!strcmp(cmd, "sweep") && nconf > 0)
        cmd_sweep(cs, nconf, warmup, measure, trials, interleave);
    else if (!strcmp(cmd, "sample") && nconf == 1)
        cmd_sample(&cs[0], interval, duration);
    else if (!strcmp(cmd, "init")) {
        if (!ncounts)
            counts[ncounts++] = G.init_threads;
        cmd_init(counts, ncounts, trials, fraction, init_qos);
    } else if (!strcmp(cmd, "verify") && nconf == 1)
        cmd_verify(&cs[0], nonces);
    else if (!strcmp(cmd, "serve") && top && argc == 1) {
        char *line = NULL, *tok[256];
        size_t cap = 0;
        while (getline(&line, &cap, stdin) > 0) {
            int n = 0;
            for (char *save = NULL, *t = strtok_r(line, " \t\r\n", &save); t && n < 256; t = strtok_r(NULL, " \t\r\n", &save))
                tok[n++] = t;
            if (n)
                command(n, tok, 0);
        }
        free(line);
    } else
        die("bad command or wrong number of CONFIGs", cmd);
    printf("{\"type\":\"end\"}\n");
    fflush(stdout);
    free(cs);
}

int main(int argc, char **argv) {
    int i = 1;
    G.init_threads = (int)sysconf(_SC_NPROCESSORS_ONLN);
    G.lib_flags = randomx_get_flags();
    for (; i + 1 < argc && !strncmp(argv[i], "--", 2); i += 2) {
        if (!strcmp(argv[i], "--seed"))
            G.seed_value = (int32_t)strtol(argv[i + 1], NULL, 10);
        else if (!strcmp(argv[i], "--init-threads"))
            G.init_threads = atoi(argv[i + 1]);
        else
            die("unknown global option", argv[i]);
    }
    if (i >= argc)
        die("usage: rxbench [--seed N] [--init-threads N] info|sweep|sample|init|verify|serve [options] [CONFIG...]",
            NULL);
    if (G.init_threads < 1 || G.init_threads > MAX_THREADS)
        die("--init-threads must be in 1..64", NULL);
    store32(G.seed, (uint32_t)G.seed_value);
    command(argc - i, argv + i, 1);
    for (int j = 0; j < 2; j++)
        if (G.cache[j])
            randomx_release_cache(G.cache[j]);
    if (G.dataset)
        randomx_release_dataset(G.dataset);
    return 0;
}
