import { build } from 'esbuild';
import { readFileSync, writeFileSync, mkdirSync, readdirSync, copyFileSync, rmSync } from 'node:fs';
import { dirname, resolve, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

const root = dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
const version = pkg.dependencies.mermaid;
if (!/^\d+\.\d+\.\d+$/.test(version)) throw new Error('Use an exact Mermaid version');
const output = resolve(root, `../../apps/assistant/ultimate_assistant/ui/vendor/mermaid@${version}`);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const slash = path => path.split(sep).join('/');
rmSync(output, { recursive: true, force: true });
mkdirSync(resolve(output, 'dist'), { recursive: true });
const yamlModule = 'node_modules/mermaid/dist/chunks/mermaid.core/chunk-LNGE3PJU.mjs';
const yamlHash = '2cb4e5f8d5fd99daccb0135fda6a5583c91f39efb0bb845af9d968ed427efcb2';
if (hash(readFileSync(resolve(root, yamlModule))) !== yamlHash) {
  throw new Error('Upstream YAML module changed; review the module adapter before rebuilding');
}
const parserSource = JSON.parse(readFileSync(resolve(root, 'upstream/parser/source-info.json'), 'utf8'));
const result = await build({
  absWorkingDir: root,
  entryPoints: ['entrypoint.mjs'],
  outfile: resolve(output, 'dist/mermaid.min.js'),
  bundle: true, format: 'iife', platform: 'browser', target: ['chrome90'],
  minify: true, keepNames: true, sourcemap: false, legalComments: 'inline',
  external: ['fs', 'path', 'require'],
  define: { 'process.env.NODE_ENV': '"production"' },
  metafile: true,
  alias: { '@mermaid-js/parser': resolve(root, 'upstream/parser/src/index.ts') },
  plugins: [{ name: 'patched-yaml-module', setup(builder) {
    builder.onLoad({ filter: /[/\\]chunk-LNGE3PJU\.mjs$/ }, args => {
      if (resolve(args.path) !== resolve(root, yamlModule)) throw new Error('Unexpected YAML module');
      return { contents: "export { JSON_SCHEMA, load } from 'js-yaml';", loader: 'js', resolveDir: root };
    });
  } }],
});
if (!Object.keys(result.metafile.inputs).includes('node_modules/mermaid/dist/mermaid.core.mjs')) {
  throw new Error('Bundle the dependency-externalized core, not the upstream prebuilt browser bundle');
}
const packages = new Map();
const embedded = new Map();
for (const input of Object.keys(result.metafile.inputs)) {
  const parts = input.replaceAll('\\', '/').split('/');
  const index = parts.lastIndexOf('node_modules');
  if (index < 0) continue;
  const length = parts[index + 1].startsWith('@') ? 2 : 1;
  const directory = resolve(root, ...parts.slice(0, index + 1 + length));
  const metadata = JSON.parse(readFileSync(resolve(directory, 'package.json'), 'utf8'));
  const key = `${metadata.name}@${metadata.version}`;
  if (!packages.has(key)) {
    const notices = readdirSync(directory).filter(name => /^(?:licen[sc]e|copying|notice)(?:\.|$)/i.test(name));
    const destination = resolve(output, 'licenses', metadata.name.replaceAll('/', '__'), metadata.version);
    mkdirSync(destination, { recursive: true });
    // Some npm packages (e.g. FastDom) retain their full license in README only.
    if (!notices.length) {
      const readme = readdirSync(directory).find(name => /^readme(?:\.|$)/i.test(name));
      if (!readme || !/## License[\s\S]*Permission is hereby granted/.test(readFileSync(resolve(directory, readme), 'utf8'))) {
        throw new Error(`Missing upstream license text for ${key}`);
      }
      notices.push(readme);
    }
    for (const notice of notices) copyFileSync(resolve(directory, notice), resolve(destination, notice));
    packages.set(key, { name: metadata.name, version: metadata.version, license: metadata.license,
      source: `https://registry.npmjs.org/${metadata.name}/${metadata.version}`, notices,
      files: [] });
  }
  packages.get(key).files.push(slash(relative(directory, resolve(root, input))));
  if (/\.(?:mjs|js)$/.test(input) && input !== yamlModule) {
    const source = readFileSync(resolve(root, input), 'utf8');
    for (const match of source.matchAll(/\.pnpm\/([^/]+?)@([0-9][^/]*)\/node_modules\//g)) {
      const name = match[1].replaceAll('+', '/');
      const ver = match[2].split('_')[0];
      embedded.set(`${name}@${ver}`, { name, version: ver });
    }
  }
}
for (const [name, expected] of Object.entries(pkg.overrides)) {
  const used = [...packages.values()].filter(item => item.name === name);
  if (!used.length || used.some(item => item.version !== expected)) throw new Error(`Unexpected ${name} runtime version`);
}
mkdirSync(resolve(output, 'licenses/@mermaid-js__parser/1.2.1'), { recursive: true });
copyFileSync(resolve(root, 'upstream/parser/LICENSE'), resolve(output, 'licenses/@mermaid-js__parser/1.2.1/LICENSE'));
const originalLicense = resolve(root, 'node_modules/mermaid/LICENSE');
copyFileSync(originalLicense, resolve(output, 'LICENSE'));
const manifest = {
  package: 'mermaid', version, build_recipe: 'tools/renderers/build.mjs',
  package_lock_sha256: hash(readFileSync(resolve(root, 'package-lock.json'))),
  parser_source: parserSource,
  module_replacements: [{ path: yamlModule, original_sha256: yamlHash, exports: ['JSON_SCHEMA', 'load'], replacement: 'js-yaml@' + pkg.dependencies['js-yaml'] }],
  description: 'Rebundled from the published dependency-externalized core with patched runtime dependencies; all diagram loaders retained.',
  files: [{ path: 'dist/mermaid.min.js', sha256: hash(readFileSync(resolve(output, 'dist/mermaid.min.js'))) }],
  runtime_packages: [...packages.values()].sort((a, b) => a.name.localeCompare(b.name)),
  embedded_packages: [...embedded.values()].sort((a, b) => a.name.localeCompare(b.name)),
};
writeFileSync(resolve(output, 'build-manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
writeFileSync(resolve(output, 'THIRD_PARTY_NOTICES.md'),
  '# Mermaid browser bundle\n\nRebuilt from the upstream published core with the locked dependencies in `tools/renderers`.\n' +
  'The build manifest records the exact modules and versions. Upstream license files are under `licenses/`.\n' +
  'The original Mermaid MIT license is included as `LICENSE`. This is an Ultimate Assistant dependency build, not an upstream browser artifact.\n');
console.log(JSON.stringify({ version, sha256: manifest.files[0].sha256,
  runtime_packages: manifest.runtime_packages.length, embedded_packages: manifest.embedded_packages }));
