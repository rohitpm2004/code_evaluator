// API-key auth + rate limiting for the evaluation endpoint (V-04).
import crypto from "crypto";
import rateLimit from "express-rate-limit";
import logger from "../config/logger.js";

/**
 * Requires header `x-api-key` to equal process.env.API_KEY.
 * Uses a constant-time comparison to avoid timing leaks.
 * If API_KEY is unset, the server refuses requests (fail closed) and logs loudly.
 */
export function requireApiKey(req, res, next) {
  const expected = process.env.API_KEY;

  if (!expected) {
    logger.error("API_KEY is not configured — rejecting request (fail closed)");
    return res.status(503).json({ success: false, error: "Server auth not configured" });
  }

  const provided = req.get("x-api-key") || "";
  const a = Buffer.from(provided);
  const b = Buffer.from(expected);

  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
    return res.status(401).json({ success: false, error: "Unauthorized" });
  }

  return next();
}

/** Rate limiter for /evaluate — defaults to 60 requests / minute / IP, bypassing authenticated internal LMS calls. */
export const evaluateRateLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: Number(process.env.RATE_LIMIT_MAX) || 60,
  standardHeaders: true,
  legacyHeaders: false,
  skip: (req) => {
    const key = req.get("x-api-key");
    return Boolean(key && process.env.API_KEY && key === process.env.API_KEY);
  },
  message: { success: false, error: "Too many requests" }
});
