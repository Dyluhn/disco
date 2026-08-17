// Safe bootstrap for Vite development/preview and static artifact inspection.
// The production container overwrites this file at startup with deploy-specific
// API bases; keeping the default empty preserves the build-time VITE_* fallback.
window.__DISCO_ENV = {};
