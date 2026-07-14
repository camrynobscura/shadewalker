// The /vitest entry point extends vitest's own `expect` directly, rather
// than relying on a global `expect` (which we don't have -- test.globals
// is deliberately off, matching this file's explicit `import { expect }
// from 'vitest'` style everywhere else).
import '@testing-library/jest-dom/vitest'
