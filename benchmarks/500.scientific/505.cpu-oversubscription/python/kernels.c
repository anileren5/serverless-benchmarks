#include <stdlib.h>
#include <time.h>

#include <omp.h>

static double process_cpu_seconds(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &ts);
  return ts.tv_sec + ts.tv_nsec * 1e-9;
}

int omp_reported_procs(void)
{
  return omp_get_num_procs();
}

int omp_default_threads(void)
{
  return omp_get_max_threads();
}

/*
 * 2D Jacobi iteration for the Laplace equation on an n x n grid.
 * The top boundary is held at 1.0, all other boundaries at 0.0.
 * Every sweep ends with the implicit barrier of `omp for`, so the kernel
 * performs `iterations` barriers per call.
 *
 * Returns -1.0 if allocation fails, otherwise 0.0.
 */
double jacobi(int n, int iterations, int threads, double *wall_seconds,
              double *cpu_seconds, double *checksum)
{
  size_t size = (size_t)n * (size_t)n;
  double *a = calloc(size, sizeof(double));
  double *b = calloc(size, sizeof(double));
  if (a == NULL || b == NULL) {
    free(a);
    free(b);
    return -1.0;
  }
  for (int j = 0; j < n; ++j) {
    a[j] = 1.0;
    b[j] = 1.0;
  }

  double cpu_begin = process_cpu_seconds();
  double wall_begin = omp_get_wtime();

#pragma omp parallel num_threads(threads)
  for (int it = 0; it < iterations; ++it) {
    const double *src = (it % 2 == 0) ? a : b;
    double *dst = (it % 2 == 0) ? b : a;
#pragma omp for schedule(static)
    for (int i = 1; i < n - 1; ++i) {
      for (int j = 1; j < n - 1; ++j) {
        dst[(size_t)i * n + j] =
            0.25 * (src[(size_t)(i - 1) * n + j] + src[(size_t)(i + 1) * n + j] +
                    src[(size_t)i * n + j - 1] + src[(size_t)i * n + j + 1]);
      }
    }
  }

  *wall_seconds = omp_get_wtime() - wall_begin;
  *cpu_seconds = process_cpu_seconds() - cpu_begin;

  const double *result = (iterations % 2 == 0) ? a : b;
  double sum = 0.0;
  for (size_t k = 0; k < size; ++k)
    sum += result[k];
  *checksum = sum;

  free(a);
  free(b);
  return 0.0;
}

/*
 * Busy-loop `threads` threads for `seconds` of wall time and report the
 * consumed process CPU time. cpu_seconds / wall_seconds is the number of
 * CPUs the sandbox actually granted, independent of what the OS reports.
 */
void spin(int threads, double seconds, double *wall_seconds, double *cpu_seconds)
{
  double cpu_begin = process_cpu_seconds();
  double wall_begin = omp_get_wtime();

#pragma omp parallel num_threads(threads)
  {
    volatile unsigned long counter = 0;
    while (omp_get_wtime() - wall_begin < seconds)
      ++counter;
  }

  *wall_seconds = omp_get_wtime() - wall_begin;
  *cpu_seconds = process_cpu_seconds() - cpu_begin;
}
