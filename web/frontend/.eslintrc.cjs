module.exports = {
  root: true,
  env: { browser: true, es2022: true, node: true },
  extends: ['eslint:recommended', 'plugin:react/recommended', 'plugin:react/jsx-runtime', 'plugin:react-hooks/recommended'],
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true } },
  settings: { react: { version: 'detect' } },
  rules: {
    'no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    'react/prop-types': 'off', // JSDoc 注释替代；首期无 TS
    'no-console': ['warn', { allow: ['warn', 'error'] }],
  },
  ignorePatterns: ['dist/', 'node_modules/'],
};
