#!/usr/bin/env node
// 把版本号同步写入 src-tauri/tauri.conf.json / src-tauri/Cargo.toml / pyproject.toml
// (package.json / package-lock.json 由 `npm version --no-git-tag-version` 负责)
// 用法: node scripts/set-version.js <x.y.z[-prerelease]>
// 只做正则替换, 不重排文件; 找不到目标行或版本非法时报错退出, 不写任何文件。
'use strict';

const fs = require('fs');
const path = require('path');

const VERSION_RE = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;

const version = process.argv[2];
if (!version || !VERSION_RE.test(version)) {
  console.error(`Invalid or missing version: "${version || ''}"`);
  console.error('Usage: node scripts/set-version.js <x.y.z[-prerelease]>  e.g. 1.2.3 or 1.2.3-beta.1');
  process.exit(1);
}

const root = path.resolve(__dirname, '..');

const targets = [
  {
    file: 'src-tauri/tauri.conf.json',
    // 命中顶层 "version": "..." 行(缩进 2 空格), 不会误伤 bundle 内其他 "version"
    pattern: /^(\s*"version"\s*:\s*")[^"]*(")/m,
    label: 'tauri.conf.json',
  },
  {
    file: 'src-tauri/Cargo.toml',
    pattern: /^(version\s*=\s*")[^"]*(")/m,
    label: 'Cargo.toml',
  },
  {
    file: 'pyproject.toml',
    pattern: /^(version\s*=\s*")[^"]*(")/m,
    label: 'pyproject.toml',
  },
];

let failed = false;
for (const t of targets) {
  const p = path.join(root, t.file);
  const src = fs.readFileSync(p, 'utf8');
  const m = src.match(t.pattern);
  if (!m) {
    console.error(`ERROR: pattern not found in ${t.file} (expected a version entry)`);
    failed = true;
    continue;
  }
  const next = src.replace(t.pattern, `$1${version}$2`);
  if (next === src) {
    console.log(`${t.label}: already ${version}`);
    continue;
  }
  fs.writeFileSync(p, next);
  console.log(`${t.label}: ${m[0].slice(m[1].length, -m[2].length)} -> ${version}`);
}

process.exit(failed ? 1 : 0);
