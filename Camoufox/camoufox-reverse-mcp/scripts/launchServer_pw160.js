// Playwright 1.60+ compatible Camoufox server launcher.
// Official camoufox launchServer.js requires lib/browserServerImpl.js which
// was removed when the driver was bundled into lib/coreBundle.js.
//
// Usage (cwd must be playwright/driver/package):
//   node launchServer_pw160.js < base64(JSON launch options)
// Prints: Websocket endpoint: ws://...

const path = require("path");
const driverPackage = process.cwd();
const playwright = require(path.join(driverPackage, "index.js"));

function collectData() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      data += chunk;
    });
    process.stdin.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.from(data, "base64").toString()));
      } catch (e) {
        reject(e);
      }
    });
  });
}

function stripNulls(value) {
  if (Array.isArray(value)) {
    return value.map(stripNulls);
  }
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, child] of Object.entries(value)) {
      if (child === null || child === undefined) {
        continue;
      }
      out[key] = stripNulls(child);
    }
    return out;
  }
  return value;
}

collectData()
  .then(async (options) => {
    console.time("Server launched");
    console.info("Launching server...");
    const cleaned = stripNulls(options || {});
    const browserServer = await playwright.firefox.launchServer(cleaned);
    console.timeEnd("Server launched");
    console.log("Websocket endpoint:\x1b[93m", browserServer.wsEndpoint(), "\x1b[0m");
    process.stdin.resume();
  })
  .catch((error) => {
    console.error("Error launching server:", error && error.stack ? error.stack : error);
    process.exit(1);
  });
