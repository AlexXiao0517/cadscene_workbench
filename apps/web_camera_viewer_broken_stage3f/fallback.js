(function () {
  "use strict";

  window.CadsceneFallback = {
    async fetchJson(url, optional = true) {
      if (!url) return null;
      const response = await fetch(url);
      if (!response.ok) {
        if (optional) return null;
        throw new Error(`${url}: HTTP ${response.status}`);
      }
      return response.json();
    },

    async fetchText(url, optional = true) {
      if (!url) return "";
      const response = await fetch(url);
      if (!response.ok) {
        if (optional) return "";
        throw new Error(`${url}: HTTP ${response.status}`);
      }
      return response.text();
    },

    parseCsv(text) {
      if (!text || !text.trim()) return [];
      const lines = text.replace(/^\ufeff/, "").trim().split(/\r?\n/);
      const headers = lines.shift().split(",");
      return lines.map((line) => {
        const values = line.split(",");
        const row = {};
        headers.forEach((header, index) => {
          row[header] = values[index] ?? "";
        });
        return row;
      });
    },
  };
})();
