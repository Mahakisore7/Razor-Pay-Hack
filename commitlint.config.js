// GIT-WORKFLOW §2. Types and scopes match the module structure so
// `git log --grep` is useful.
//
// CommonJS, deliberately -- there is no root package.json to declare
// "type": "module", so a bare .js file here is loaded as CommonJS by
// both Node and commitlint's own config loader.
module.exports = {
  extends: ["@commitlint/config-conventional"],
  rules: {
    "type-enum": [
      2,
      "always",
      ["feat", "fix", "docs", "refactor", "test", "chore", "perf", "ci", "build", "revert"],
    ],
    "scope-enum": [
      2,
      "always",
      [
        // Code modules (ARCHITECTURE §5) -- GIT-WORKFLOW §2.2.
        "domain",
        "policy",
        "detection",
        "diagnosis",
        "planning",
        "execution",
        "attribution",
        "gateway",
        "audit",
        "bench",
        "api",
        "console",
        "infra",
        // docs/ sections (docs/NN-<name>/), for `docs(...)` commits --
        // matches actual practice, which GIT-WORKFLOW §2.2 didn't
        // document until this list was checked against `git log` while
        // wiring up commitlint (T0.8).
        "overview",
        "product",
        "technical",
        "delivery",
        "adr",
        "submission",
      ],
    ],
    "subject-case": [2, "never", ["start-case", "pascal-case", "upper-case"]],
    "header-max-length": [2, "always", 72],
  },
};
