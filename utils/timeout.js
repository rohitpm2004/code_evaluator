/**
 * timeout.js
 * Shared timeout and cancellation utilities for all evaluators and workers.
 */

/**
 * Wraps a Promise with a hard timeout.
 * If the promise does not resolve within `ms` milliseconds, it rejects with a TimeoutError.
 *
 * @param {Promise<any>} promise - The promise to race
 * @param {number} ms            - Timeout in milliseconds
 * @param {string} [label]       - Optional label for the error message
 * @returns {Promise<any>}
 */
export async function withTimeout(promise, ms, label = "operation") {
  let timeoutId;

  const timeoutPromise = new Promise((_, reject) => {
    timeoutId = setTimeout(() => {
      reject(new Error(`Timeout: "${label}" exceeded ${ms}ms`));
    }, ms);
  });

  try {
    const result = await Promise.race([promise, timeoutPromise]);
    return result;
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * Returns a Promise that resolves after `ms` milliseconds.
 *
 * @param {number} ms - Milliseconds to wait
 * @returns {Promise<void>}
 */
export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
