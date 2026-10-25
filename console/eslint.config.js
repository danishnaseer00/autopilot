import js from "@eslint/js";
import globals from "globals";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**", "coverage/**"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser },
    },
    plugins: {
      react,
      "react-hooks": reactHooks,
      "jsx-a11y": jsxA11y,
    },
    settings: {
      react: { version: "18.3" },
    },
    rules: {
      ...react.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,
      "react/react-in-jsx-scope": "off",
      "react/jsx-uses-react": "off",
      "react/prop-types": "off",
      // SPEC-37 R-19 / AC-12. This is the lint half of the no-inner-HTML guard.
      "react/no-danger": "error",
      // SPEC-37 R-18 / AC-10. No browser-persisted credential or session state.
      "no-restricted-globals": [
        "error",
        {
          name: "localStorage",
          message: "Console credentials and session state must not use localStorage (SPEC-37 R-18).",
        },
        {
          name: "sessionStorage",
          message: "Console credentials and session state must not use sessionStorage (SPEC-37 R-18).",
        },
      ],
      "no-restricted-properties": [
        "error",
        {
          object: "window",
          property: "localStorage",
          message: "Console credentials and session state must not use localStorage (SPEC-37 R-18).",
        },
        {
          object: "window",
          property: "sessionStorage",
          message: "Console credentials and session state must not use sessionStorage (SPEC-37 R-18).",
        },
      ],
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      "jsx-a11y/no-autofocus": "off",
    },
  },
  {
    files: ["**/*.test.{ts,tsx}", "src/test/**"],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
  },
  {
    files: ["*.config.{ts,js}", "scripts/**"],
    languageOptions: {
      globals: { ...globals.node },
    },
  },
);
