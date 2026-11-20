import { writeFile } from "node:fs/promises";
import sdk from "../dist/index.cjs";

const names = Object.keys(sdk).join(", ");
await writeFile(
  new URL("../dist/index.js", import.meta.url),
  `import sdk from "./index.cjs";\nexport const { ${names} } = sdk;\n`,
);
await writeFile(new URL("../dist/index.d.ts", import.meta.url), 'export * from "./index.cjs";\n');
