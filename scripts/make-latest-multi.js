#!/usr/bin/env node
/*
 * 多平台 latest.json 生成器 (make-latest.js 的三平台版)。
 *
 * 扫描 dist/, 按文件名归位三个平台的更新条目, 生成 tauri-plugin-updater
 * 消费的 latest.json (发 Release 时与产物一起上传, 更新端点
 * releases/latest/download/latest.json 恒指向最新)。
 *
 *   windows-x86_64 : x-code_<ver>_x64-setup.exe        (+ .exe.sig)
 *   darwin-aarch64 : x-code_<ver>_aarch64.app.tar.gz   (+ .sig)
 *   darwin-x86_64  : x-code_<ver>_x64.app.tar.gz       (+ .sig)
 *   linux-x86_64   : x-code_<ver>_amd64.AppImage       (+ .AppImage.sig)
 *
 * 用法: node scripts/make-latest-multi.js --tag v3.3.9 [--dist dist] [--repo a/b]
 *   tag 决定产物下载 URL; 缺 --tag 时用 --version 指定的版本号拼 v<version>。
 * 某平台文件缺失 = 该平台不出条目 (部分构建的场景, 如手动触发只跑了 Windows)。
 */
const fs = require("fs");
const path = require("path");

const args = process.argv.slice(2);
function argOf(name, fallback) {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
}

const root = path.resolve(__dirname, "..");
const dist = path.resolve(argOf("--dist", path.join(root, "dist")));
const tag = argOf("--tag");
const repo = argOf("--repo", "Steve-pixel-cpu/x-code");

// 版本取自 tag (去 v 前缀), 不读 tauri.conf.json——构建 job 里 set-version
// 同步出的新版本号只存在于各 job 工作区, 不会回写仓库 (CI 实测教训)
if (!tag) {
  console.error("[make-latest-multi] 缺少 --tag (更新 URL 需要确切的 tag 名)");
  process.exit(1);
}
const version = tag.replace(/^v/, "");

// 平台条目: 更新包 + 其 .sig (签名内容本身, 不是路径 —— updater 硬性要求)
function entry(updateFile, sigSuffix) {
  const p = path.join(dist, updateFile);
  const sig = p + sigSuffix;
  if (!fs.existsSync(p)) return null;
  if (!fs.existsSync(sig)) {
    console.warn(`[make-latest-multi] 跳过 ${updateFile}: 无 ${sigSuffix} (未签名构建)`);
    return null;
  }
  return {
    signature: fs.readFileSync(sig, "utf8").trim(),
    url: `https://github.com/${repo}/releases/download/${tag}/${updateFile}`,
  };
}

const platforms = {};
const win = entry(`x-code_${version}_x64-setup.exe`, ".sig");
if (win) platforms["windows-x86_64"] = win;
for (const [arch, key] of [["aarch64", "darwin-aarch64"], ["x64", "darwin-x86_64"]]) {
  const e = entry(`x-code_${version}_${arch}.app.tar.gz`, ".sig");
  if (e) platforms[key] = e;
}
const linux = entry(`x-code_${version}_amd64.AppImage`, ".sig");
if (linux) platforms["linux-x86_64"] = linux;

if (Object.keys(platforms).length === 0) {
  console.error("[make-latest-multi] 没有找到任何带签名的更新包, 不生成 latest.json");
  process.exit(1);
}

const manifest = {
  version,
  notes: `x-code v${version}`,
  pub_date: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
  platforms,
};

const out = path.join(dist, "latest.json");
fs.writeFileSync(out, JSON.stringify(manifest, null, 2) + "\n");
console.log(`[make-latest-multi] wrote ${out}: ${Object.keys(platforms).join(", ")}`);
