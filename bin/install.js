#!/usr/bin/env node
/**
 * Installer for the growth-recon agent skill.
 *
 *   npx github:brenoepics/growth-recon-skill            -> ~/.claude/skills/growth-recon
 *   npx github:brenoepics/growth-recon-skill --project   -> ./.claude/skills/growth-recon
 *
 * Flags: --project  install into the current project instead of the home dir
 *        --force    overwrite an existing install
 *        --dry-run  print what would happen and exit
 *        --dir <p>  install into an explicit directory
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const SKILL_NAME = 'growth-recon';
// Only what the skill needs at runtime. The repo's README, LICENSE, evals and
// this installer are deliberately left out so the installed skill stays lean.
const PAYLOAD = ['SKILL.md', 'references', 'scripts', 'examples'];

const argv = process.argv.slice(2);
const has = (f) => argv.includes(f);
const valueOf = (f) => {
  const i = argv.indexOf(f);
  return i !== -1 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : null;
};

const c = process.stdout.isTTY
  ? { b: '\x1b[1m', dim: '\x1b[2m', g: '\x1b[32m', y: '\x1b[33m', r: '\x1b[31m', x: '\x1b[0m' }
  : { b: '', dim: '', g: '', y: '', r: '', x: '' };

const say = (m = '') => console.log(m);
const ok = (m) => say(`${c.g}✓${c.x} ${m}`);
const warn = (m) => say(`${c.y}!${c.x} ${m}`);
const die = (m, hint) => {
  console.error(`${c.r}✗${c.x} ${m}`);
  if (hint) console.error(`  ${c.dim}${hint}${c.x}`);
  process.exit(1);
};

if (has('--help') || has('-h')) {
  say(`
${c.b}growth-recon${c.x} — install the skill

  npx github:brenoepics/growth-recon-skill [options]

  ${c.b}--project${c.x}   install into ./.claude/skills instead of your home directory
  ${c.b}--force${c.x}     overwrite an existing install
  ${c.b}--dir <path>${c.x} install to this exact directory
  ${c.b}--dry-run${c.x}   show what would happen, change nothing
`);
  process.exit(0);
}

const repoRoot = path.resolve(__dirname, '..');

// Resolve where the skill goes. --dir names the skill directory itself; the
// other modes name a skills *root* that the skill name is appended to.
let target;
if (valueOf('--dir')) {
  target = path.resolve(valueOf('--dir'));
} else if (has('--project')) {
  target = path.join(process.cwd(), '.claude', 'skills', SKILL_NAME);
} else {
  const home = os.homedir();
  if (!home) die('Could not determine your home directory.', 'Pass --dir <path> instead.');
  target = path.join(home, '.claude', 'skills', SKILL_NAME);
}

say(`\n${c.b}growth-recon${c.x} ${c.dim}→ ${target}${c.x}\n`);

// Verify the payload is actually present before touching anything on disk.
const missing = PAYLOAD.filter((p) => !fs.existsSync(path.join(repoRoot, p)));
if (missing.length) {
  die(`This package is incomplete — missing: ${missing.join(', ')}`,
      'If you cloned the repo, run the installer from its root.');
}

if (has('--dry-run')) {
  say('Would copy:');
  PAYLOAD.forEach((p) => say(`  ${p}  ${c.dim}→ ${path.join(target, p)}${c.x}`));
  say(`\n${c.dim}Nothing was changed (--dry-run).${c.x}\n`);
  process.exit(0);
}

// Don't clobber someone's edited copy without being told to.
if (fs.existsSync(target) && !has('--force')) {
  const existing = fs.readdirSync(target);
  if (existing.length) {
    die(`${target} already exists and is not empty.`,
        'Re-run with --force to overwrite it, or --dir <path> to install elsewhere.');
  }
}

try {
  fs.mkdirSync(target, { recursive: true });
  for (const item of PAYLOAD) {
    const from = path.join(repoRoot, item);
    const to = path.join(target, item);
    fs.rmSync(to, { recursive: true, force: true });
    fs.cpSync(from, to, { recursive: true });
  }
} catch (err) {
  die(`Could not write to ${target}: ${err.message}`,
      'Check permissions, or use --dir <path> to pick a writable location.');
}

const files = (function count(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).reduce((n, e) => {
    const p = path.join(dir, e.name);
    return n + (e.isDirectory() ? count(p) : 1);
  }, 0);
})(target);

ok(`Installed ${files} files`);

// Python is what the scripts actually run on, so check it rather than assume.
let py = null;
for (const candidate of ['python3', 'python']) {
  try {
    const v = execFileSync(candidate, ['--version'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    py = `${candidate} (${v.trim()})`;
    break;
  } catch (_) { /* keep looking */ }
}
if (py) {
  ok(`Python found: ${py}`);
} else {
  warn('Python 3 was not found on your PATH.');
  say(`  ${c.dim}The skill's scripts need Python 3.8+ (standard library only).${c.x}`);
}

say(`
${c.b}Next${c.x}

  Ask your agent, in plain language:

    ${c.dim}Do a full competitive teardown of https://monday.com${c.x}
    ${c.dim}What is linear.app A/B testing right now?${c.x}

  Or run the scripts directly:

    ${c.dim}python3 -I ${path.join(target, 'scripts', 'recon.py')} https://example.com${c.x}
    ${c.dim}python3 -I ${path.join(target, 'scripts', 'gtm_parse.py')} GTM-XXXXXXX${c.x}

  ${c.dim}Reads only public, unauthenticated files. Docs: https://github.com/brenoepics/growth-recon-skill${c.x}
`);
