"use strict";

// Number formatting shared by the live and history pages.
window.IftopFormat = {
  // A rate in bytes/s, shown as bits/s (1000-based) or bytes/s (1024-based).
  rate(bytesPerSec, bits) {
    let v = bits ? bytesPerSec * 8 : bytesPerSec;
    const base = bits ? 1000 : 1024;
    const units = bits ? ["b", "Kb", "Mb", "Gb", "Tb"] : ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (v >= base && i < units.length - 1) { v /= base; i++; }
    const digits = v >= 100 || i === 0 ? 0 : v >= 10 ? 1 : 2;
    return v.toFixed(digits) + units[i];
  },

  bytes(n) {
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(i === 0 ? 0 : v >= 100 ? 0 : 1) + units[i];
  },
};
