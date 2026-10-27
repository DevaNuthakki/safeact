// Fail if two files differ (used to prove the generated API types are current).
import { readFileSync, rmSync } from "node:fs";

const [expected, actual] = process.argv.slice(2);
const same = readFileSync(expected, "utf8") === readFileSync(actual, "utf8");
rmSync(actual);
if (!same) {
  console.error(`${expected} is out of date: run npm run gen:api`);
  process.exit(1);
}
