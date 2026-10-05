import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import vue from "eslint-plugin-vue";
import tsParser from "@typescript-eslint/parser";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  ...vue.configs["flat/essential"],
  {files:["**/*.vue"],languageOptions:{parserOptions:{parser:tsParser,extraFileExtensions:[".vue"]}}},
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    "node_modules/**", "dist/**", ".npm-cache/**", ".wrangler/**", ".sites-runtime/**", "examples/**", "scripts/**", "components/ui/**",
  ]),
  {
    files: ["components/ui/**/*.{ts,tsx}", "hooks/use-mobile.ts"],
    rules: {
      // These files are vendored verbatim from shadcn@4.17.0. Keep the
      // registry source intact while applying the stricter rules to Site code.
      "@typescript-eslint/no-unused-vars": "off",
      "react-hooks/purity": "off",
      "react-hooks/set-state-in-effect": "off",
    },
  },
]);

export default eslintConfig;
