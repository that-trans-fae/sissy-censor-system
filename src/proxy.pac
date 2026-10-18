// Route everything through the censor proxy, falling back to a direct
// connection if it is not running (fail open when the container is down).
function FindProxyForURL(url, host) {
  if (isPlainHostName(host) || host === "localhost" || shExpMatch(host, "127.*") || host === "[::1]") {
    return "DIRECT";
  }
  return "PROXY 127.0.0.1:8080; DIRECT";
}
