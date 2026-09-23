/*
 * Cost of writing the floating-point control register (rounding mode).
 *
 * RandomX v1 rewrites the rounding mode on nearly every CFROUND (about once
 * per VM iteration); v2 only when CFROUND's filter passes. This measures the
 * marginal cost of one write inside a dependent FP loop, three ways:
 *   base    - dependent FP adds only
 *   same    - plus a write of the unchanged mode every iteration
 *   toggle  - plus a write alternating round-to-nearest / round-down
 * Prints one JSON object. arm64 writes FPCR; x86-64 writes MXCSR.
 *
 *   cc -O2 -o build/fpcr_bench tools/fpcr_bench.c && build/fpcr_bench [iterations] [repeats]
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#if defined(__APPLE__)
#include <pthread.h>
#include <sys/qos.h>
#endif

#if defined(__aarch64__)
static inline uint64_t get_ctl(void) { uint64_t v; __asm__ volatile("mrs %0, fpcr" : "=r"(v)); return v; }
static inline void set_ctl(uint64_t v) { __asm__ volatile("msr fpcr, %0" : : "r"(v)); }
#define RMODE_SHIFT 22
#define RMODE_DOWN 2ull
#define RMODE_MASK 3ull
#elif defined(__x86_64__)
static inline uint64_t get_ctl(void) { uint32_t v; __asm__ volatile("stmxcsr %0" : "=m"(v)); return v; }
static inline void set_ctl(uint64_t v) { uint32_t x = (uint32_t)v; __asm__ volatile("ldmxcsr %0" : : "m"(x)); }
#define RMODE_SHIFT 13
#define RMODE_DOWN 1ull
#define RMODE_MASK 3ull
#else
#error "unsupported architecture"
#endif

static double now(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec * 1e-9;
}

/* volatile inputs keep the compiler from folding the loop */
static volatile double seed_a = 1.0000001, seed_b = 1e-9;

static double run(long iters, int mode, uint64_t nearest, uint64_t down) {
	double a = seed_a, b = seed_b;
	uint64_t ctl[2] = {nearest, mode == 2 ? down : nearest};
	for (long i = 0; i < iters; ++i) {
		if (mode)
			set_ctl(ctl[i & 1]);
		a = a + b;
		a = a + b;
		a = a + b;
		a = a + b;
	}
	set_ctl(nearest);
	return a;
}

int main(int argc, char **argv) {
	long iters = argc > 1 ? atol(argv[1]) : 20000000L;
	int repeats = argc > 2 ? atoi(argv[2]) : 7;
#if defined(__APPLE__)
	pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
#endif
	uint64_t nearest = get_ctl() & ~(RMODE_MASK << RMODE_SHIFT);
	uint64_t down = nearest | (RMODE_DOWN << RMODE_SHIFT);
	const char *names[3] = {"base", "same", "toggle"};
	double best[3] = {1e30, 1e30, 1e30}, sink = 0;
	run(iters / 10, 0, nearest, down);
	for (int r = 0; r < repeats; ++r)
		for (int m = 0; m < 3; ++m) {
			double t0 = now();
			sink += run(iters, m, nearest, down);
			double ns = (now() - t0) * 1e9 / iters;
			if (ns < best[m])
				best[m] = ns;
		}
	printf("{\"iterations\": %ld, \"repeats\": %d, \"statistic\": \"min over repeats\", ", iters, repeats);
	for (int m = 0; m < 3; ++m)
		printf("\"%s_ns_per_iter\": %.4f, ", names[m], best[m]);
	printf("\"same_write_ns\": %.4f, \"toggle_write_ns\": %.4f, \"sink\": %g}\n",
	       best[1] - best[0], best[2] - best[0], sink);
	return 0;
}
