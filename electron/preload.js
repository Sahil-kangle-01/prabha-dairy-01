/**
 * Preload script — exposes nothing for now.
 * Context isolation is enabled; this is the safe bridge layer
 * if we ever need to expose Node APIs to the renderer.
 */
const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("prabha", {
  version: "1.0.0",
  isElectron: true,
});
