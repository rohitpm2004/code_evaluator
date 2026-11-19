// SSRF / URL safety guard (V-03).
//
// - Allows only http(s) (blocks file://, gopher://, etc.).
// - Resolves the hostname and rejects loopback / private / link-local targets
//   (incl. cloud metadata 169.254.169.254).
// - For git repos, additionally enforces an allowlist of hosts.
//
// Used by the controller (fail fast) and again right before any clone/navigation.
import dns from "dns/promises";

function ipv4ToInt(ip) {
  const parts = ip.split(".").map(Number);
  if (parts.length !== 4 || parts.some(n => Number.isNaN(n) || n < 0 || n > 255)) {
    return null;
  }
  return ((parts[0] << 24) >>> 0) + (parts[1] << 16) + (parts[2] << 8) + parts[3];
}

function inRange(ipInt, cidrBase, bits) {
  const baseInt = ipv4ToInt(cidrBase);
  const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
  return (ipInt & mask) === (baseInt & mask);
}

function isPrivateIPv4(ip) {
  const n = ipv4ToInt(ip);
  if (n === null) return false;
  return (
    inRange(n, "0.0.0.0", 8) ||
    inRange(n, "10.0.0.0", 8) ||
    inRange(n, "100.64.0.0", 10) ||   // CGNAT
    inRange(n, "127.0.0.0", 8) ||     // loopback
    inRange(n, "169.254.0.0", 16) ||  // link-local + metadata
    inRange(n, "172.16.0.0", 12) ||
    inRange(n, "192.168.0.0", 16) ||
    inRange(n, "198.18.0.0", 15) ||   // benchmarking
    inRange(n, "224.0.0.0", 4) ||     // multicast
    inRange(n, "240.0.0.0", 4)        // reserved
  );
}

function isPrivateIPv6(ip) {
  const addr = ip.toLowerCase();
  if (addr === "::1" || addr === "::") return true;
  if (addr.startsWith("fe80")) return true;            // link-local
  if (addr.startsWith("fc") || addr.startsWith("fd")) return true; // ULA fc00::/7
  // IPv4-mapped (::ffff:a.b.c.d)
  const mapped = addr.match(/::ffff:(\d+\.\d+\.\d+\.\d+)/);
  if (mapped) return isPrivateIPv4(mapped[1]);
  return false;
}

function isPrivateAddress(ip, family) {
  return family === 6 ? isPrivateIPv6(ip) : isPrivateIPv4(ip);
}

/**
 * Validate a URL is safe to fetch/clone. Resolves DNS and checks every
 * resolved address. Throws a descriptive Error if unsafe.
 *
 * @param {string} urlStr
 * @param {{ allowedHosts?: string[] }} [opts]
 * @returns {Promise<URL>}
 */
export async function assertSafeUrl(urlStr, opts = {}) {
  let url;
  try {
    url = new URL(urlStr);
  } catch {
    throw new Error(`Invalid URL: ${urlStr}`);
  }

  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`Disallowed URL scheme: ${url.protocol}`);
  }

  const host = url.hostname;

  // Optional host allowlist (git repos)
  if (opts.allowedHosts && opts.allowedHosts.length > 0) {
    const ok = opts.allowedHosts.some(
      h => host === h || host.endsWith(`.${h}`)
    );
    if (!ok) {
      throw new Error(`Host not in allowlist: ${host}`);
    }
  }

  // Resolve and validate every address the host points to.
  let addresses;
  try {
    addresses = await dns.lookup(host, { all: true });
  } catch {
    throw new Error(`DNS resolution failed for host: ${host}`);
  }

  for (const { address, family } of addresses) {
    if (isPrivateAddress(address, family)) {
      throw new Error(`Refusing to access private/loopback address: ${address}`);
    }
  }

  return url;
}

/**
 * Fast synchronous check (scheme + optional host allowlist, no DNS).
 * Use for fail-fast validation of many URLs at request time; the full
 * DNS-resolving assertSafeUrl still runs right before clone/navigation.
 * Throws on failure.
 */
export function assertUrlSyntax(urlStr, opts = {}) {
  let url;
  try {
    url = new URL(urlStr);
  } catch {
    throw new Error(`Invalid URL: ${urlStr}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`Disallowed URL scheme: ${url.protocol}`);
  }
  if (opts.allowedHosts && opts.allowedHosts.length > 0) {
    const host = url.hostname;
    const ok = opts.allowedHosts.some(h => host === h || host.endsWith(`.${h}`));
    if (!ok) throw new Error(`Host not in allowlist: ${host}`);
  }
  return url;
}

/** Comma-separated ALLOWED_GIT_HOSTS env → array (defaults to common hosts). */
export function getAllowedGitHosts() {
  const raw = process.env.ALLOWED_GIT_HOSTS || "github.com,gitlab.com,bitbucket.org,github.io,raw.githubusercontent.com,githubusercontent.com";
  return raw.split(",").map(s => s.trim()).filter(Boolean);
}
