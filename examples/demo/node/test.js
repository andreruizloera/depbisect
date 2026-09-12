// The demo app's one test: padstr must right-align a value to a width.
const pad = require("padstr");

const got = pad("7", 3);
if (got !== "  7") {
  console.error(`expected "  7", got ${JSON.stringify(got)}`);
  process.exit(1);
}
