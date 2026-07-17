(function (root, factory) {
  const exports = factory();
  if (typeof module === "object" && module.exports) module.exports = exports;
  if (root) root.VideoDisplayTransform = exports.VideoDisplayTransform;
})(typeof window !== "undefined" ? window : globalThis, function () {
  class VideoDisplayTransform {
    constructor(videoWidth = 0, videoHeight = 0, containerWidth = 0, containerHeight = 0) {
      this.update(videoWidth, videoHeight, containerWidth, containerHeight);
    }

    update(videoWidth, videoHeight, containerWidth, containerHeight) {
      this.videoWidth = Math.max(0, Number(videoWidth) || 0);
      this.videoHeight = Math.max(0, Number(videoHeight) || 0);
      this.containerWidth = Math.max(0, Number(containerWidth) || 0);
      this.containerHeight = Math.max(0, Number(containerHeight) || 0);
      this.scale = this.videoWidth && this.videoHeight
        ? Math.min(this.containerWidth / this.videoWidth, this.containerHeight / this.videoHeight)
        : 0;
      this.displayWidth = this.videoWidth * this.scale;
      this.displayHeight = this.videoHeight * this.scale;
      this.offsetX = (this.containerWidth - this.displayWidth) / 2;
      this.offsetY = (this.containerHeight - this.displayHeight) / 2;
      return this;
    }

    sourceToDisplay(point) {
      if (!this.scale) return null;
      return {
        x: this.offsetX + Number(point.x) * this.scale,
        y: this.offsetY + Number(point.y) * this.scale,
      };
    }

    displayToSource(point) {
      if (!this.scale) return null;
      const x = Number(point.x);
      const y = Number(point.y);
      if (
        x < this.offsetX || x > this.offsetX + this.displayWidth
        || y < this.offsetY || y > this.offsetY + this.displayHeight
      ) return null;
      return {
        x: (x - this.offsetX) / this.scale,
        y: (y - this.offsetY) / this.scale,
      };
    }
  }

  return { VideoDisplayTransform };
});
