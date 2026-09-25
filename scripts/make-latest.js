#!/usr/bin/env node
/*
 * Generate dist/latest.json - the static update manifest for
 * tauri-plugin-updater.
 *
 * Reads the version from src-tauri/tauri.conf.json and picks up the NSIS
 * installer + its .sig signature from dist/ (produced by `tauri build` with
 * bundle.createUpdaterArtifacts = true), then writes:
 *
 *   {
 *     "version": "3.4.0",
 *     "notes": "...",
 *     "pub_date": "2026-09-25T12:00:00Z",
 *     "platforms": {
 *       "windows-x86_64": {
 *         "signature": "<contents of the .sig file (required, NOT a path)>",
 *         "url": "https://github.com/.../releases/download/v3.4.0/x-code_3.4.0_x64-setup.exe"
 *       }
 *     }
 *   }
 *
 * Usage: node scripts/make-latest.js [notes]
 *   notes: release notes, defaults to "x-code <ver>"
 *
 * Prereq: build-exe.cmd ran the packaging under a signing key (otherwise
 * there is no .sig file). The key lives in .tauri/x-code.key and must never
 * be committed. Upload dist/latest.json together with the installer to the
 * release; the app's updater endpoint (releases/latest/download/latest.json)
 * always resolves to the newest release.
 */
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const dist = path.join(root, "dist");
const conf = JSON.parse(
  fs.readFileSync(path.join(root, "src-tauri", "tauri.conf.json"), "utf8")
);
const version = conf.version;
const notes = process.argv[2] || `x-code v${version}`;

// NSIS installer: dist/x-code_<ver>_x64-setup.exe + its .sig sidecar
const exe = `x-code_${version}_x64-setup.exe`;
const exePath = path.join(dist, exe);
const sigPath = exePath + ".sig";

if (!fs.existsSync(exePath)) {
  console.error(`[make-latest] ${exe} not found - run "build-exe.cmd tauri" first`);
  process.exit(1);
}
if (!fs.existsSync(sigPath)) {
  console.error(
    `[make-latest] ${exe}.sig not found - the build ran without a signing key.\n` +
      `  The key should be .tauri/x-code.key (never commit it); build-exe.cmd injects\n` +
      `  it plus the password from .tauri/x-code.key.password automatically. Without\n` +
      `  a signature tauri generates no .sig and this version cannot be an update target.`
  );
  process.exit(1);
}

// GitHub asset download URL (tag and asset names are a fixed convention:
// v<version> / x-code_<version>_x64-setup.exe)
const repo = "Steve-pixel-cpu/x-code";
const url = `https://github.com/${repo}/releases/download/v${version}/${exe}`;

const manifest = {
  version,
  notes,
  pub_date: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
  platforms: {
    "windows-x86_64": {
      signature: fs.readFileSync(sigPath, "utf8").trim(),
      url,
    },
  },
};

const out = path.join(dist, "latest.json");
fs.writeFileSync(out, JSON.stringify(manifest, null, 2) + "\n");
console.log(`[make-latest] wrote ${out}`);
console.log(`[make-latest]   version  : ${version}`);
console.log(`[make-latest]   url      : ${url}`);
console.log(
  `[make-latest]   signature: ${manifest.platforms["windows-x86_64"].signature.length} bytes`
);
console.log(
  `[make-latest] Publish: upload ${exe} + latest.json to release v${version} (scripts/publish.cmd)`
);
