import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { schema } from './node_modules/langium-cli/lib/generator/node-util.js';
import { generate } from './node_modules/langium-cli/lib/generate.js';

// Supply the schema's canonical base URI for jsonschema/modern Node URL resolution.
// Grammar validation stays enabled and upstream source files stay unchanged.
(await schema).id = 'https://langium.org/langium-config.schema.json';
process.chdir(resolve(dirname(fileURLToPath(import.meta.url)), 'upstream/parser'));
if (!await generate({ file: 'langium-config.json' })) process.exit(1);
