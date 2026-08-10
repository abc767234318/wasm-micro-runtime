import { cp, mkdir, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const projectRoot = fileURLToPath(new URL("../", import.meta.url));
const source = fileURLToPath(new URL("../site/", import.meta.url));
const destination = fileURLToPath(new URL("../public/spec/", import.meta.url));

await rm(destination, { recursive: true, force: true });
await mkdir(destination, { recursive: true });
await cp(source, destination, { recursive: true, force: true });
console.log(`Prepared static specification: ${source} -> ${destination}`);
