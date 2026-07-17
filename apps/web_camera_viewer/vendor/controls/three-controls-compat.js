(function () {
  "use strict";

  if (window.OrbitControls && window.THREE && !window.THREE.OrbitControls) {
    window.THREE.OrbitControls = window.OrbitControls;
  }
  if (window.TransformControls && window.THREE && !window.THREE.TransformControls) {
    window.THREE.TransformControls = window.TransformControls;
  }
})();
