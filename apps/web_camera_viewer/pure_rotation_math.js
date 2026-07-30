(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CadscenePureRotationMath = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
  const dot=(a,b)=>a.reduce((s,v,i)=>s+v*b[i],0);
  const normalize=(q)=>{const n=Math.hypot(...q);return q.map(v=>v/n);};
  function matrixToQuaternion(m){const tr=m[0][0]+m[1][1]+m[2][2];let q;if(tr>0){const s=Math.sqrt(tr+1)*2;q=[s/4,(m[2][1]-m[1][2])/s,(m[0][2]-m[2][0])/s,(m[1][0]-m[0][1])/s];}else{let i=0;if(m[1][1]>m[0][0])i=1;if(m[2][2]>m[i][i])i=2;const j=(i+1)%3,k=(i+2)%3,s=Math.sqrt(1+m[i][i]-m[j][j]-m[k][k])*2;q=[(m[k][j]-m[j][k])/s,0,0,0];q[i+1]=s/4;q[j+1]=(m[j][i]+m[i][j])/s;q[k+1]=(m[k][i]+m[i][k])/s;}return normalize(q);}
  function quaternionToMatrix(q){const [w,x,y,z]=normalize(q);return [[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],[2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],[2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]];}
  function slerp(a,b,t){let q1=normalize(a),q2=normalize(b),c=dot(q1,q2);if(c<0){q2=q2.map(v=>-v);c=-c;}if(c>.9995)return normalize(q1.map((v,i)=>v+t*(q2[i]-v)));const angle=Math.acos(clamp(c,-1,1)),s=Math.sin(angle);return q1.map((v,i)=>(Math.sin((1-t)*angle)*v+Math.sin(t*angle)*q2[i])/s);}
  const rotationOf=(p)=>p.rotation_cad_from_camera||p.rotation_local_from_camera;
  const centerOf=(p)=>p.camera_center_web||p.camera_center_local||[0,0,0];
  function poseAtPts(poses,time){if(!poses.length)return null;if(time<=Number(poses[0].pts_time_sec))return {...poses[0],camera_center_web:centerOf(poses[0])};if(time>=Number(poses.at(-1).pts_time_sec))return {...poses.at(-1),camera_center_web:centerOf(poses.at(-1))};const right=poses.findIndex(p=>Number(p.pts_time_sec)>=time),a=poses[right-1],b=poses[right];if(a.segment_id!==b.segment_id)return null;const t=(time-Number(a.pts_time_sec))/(Number(b.pts_time_sec)-Number(a.pts_time_sec));return {...a,pts_time_sec:time,rotation_cad_from_camera:quaternionToMatrix(slerp(matrixToQuaternion(rotationOf(a)),matrixToQuaternion(rotationOf(b)),t)),camera_center_web:centerOf(a)};}
  function matrixToViewerEuler(m){const right=[m[0][0],m[1][0],m[2][0]],forward=[m[0][2],m[1][2],m[2][2]],yaw=Math.atan2(forward[0],forward[1]),pitch=Math.asin(clamp(forward[2],-1,1)),baseRight=[Math.cos(yaw),-Math.sin(yaw),0],baseDown=[Math.sin(pitch)*Math.sin(yaw),Math.sin(pitch)*Math.cos(yaw),-Math.cos(pitch)],roll=Math.atan2(-dot(right,baseDown),dot(right,baseRight)),scale=180/Math.PI;return {yaw:yaw*scale,pitch:pitch*scale,roll:roll*scale};}
  function viewerEulerToMatrix(value){const scale=Math.PI/180,yaw=Number(value.yaw||0)*scale,pitch=Number(value.pitch||0)*scale,roll=Number(value.roll||0)*scale,forward=[Math.cos(pitch)*Math.sin(yaw),Math.cos(pitch)*Math.cos(yaw),Math.sin(pitch)],baseRight=[Math.cos(yaw),-Math.sin(yaw),0],baseDown=[Math.sin(pitch)*Math.sin(yaw),Math.sin(pitch)*Math.cos(yaw),-Math.cos(pitch)],right=baseRight.map((v,i)=>v*Math.cos(roll)-baseDown[i]*Math.sin(roll)),down=baseDown.map((v,i)=>v*Math.cos(roll)+baseRight[i]*Math.sin(roll));return [[right[0],down[0],forward[0]],[right[1],down[1],forward[1]],[right[2],down[2],forward[2]]];}
  function localRotationToViewerMatrix(rotation){const basis=[[1,0,0],[0,0,1],[0,-1,0]];return basis.map(row=>[0,1,2].map(column=>row.reduce((sum,value,index)=>sum+value*rotation[index][column],0)));}
  const transpose=(m)=>[0,1,2].map(row=>[0,1,2].map(column=>m[column][row]));
  const multiply=(a,b)=>a.map(row=>[0,1,2].map(column=>row.reduce((sum,value,index)=>sum+value*b[index][column],0)));
  function axisAngleMatrix(axis,angleDeg){const [x,y,z]=normalize(axis.map(Number)),a=Number(angleDeg)*Math.PI/180,c=Math.cos(a),s=Math.sin(a),t=1-c;return [[t*x*x+c,t*x*y-s*z,t*x*z+s*y],[t*x*y+s*z,t*y*y+c,t*y*z-s*x],[t*x*z-s*y,t*y*z+s*x,t*z*z+c]];}
  function rotateAboutWorldUp(rotation,angleDeg,upAxis=[0,0,1]){return multiply(axisAngleMatrix(upAxis,angleDeg),rotation);}
  function localCameraDeltaMatrix(value={}){const yaw=axisAngleMatrix([0,-1,0],Number(value.yaw)||0),pitch=axisAngleMatrix([1,0,0],Number(value.pitch)||0),roll=axisAngleMatrix([0,0,1],Number(value.roll)||0);return multiply(multiply(yaw,pitch),roll);}
  function applyLocalCameraDelta(rotation,value={}){return multiply(rotation,localCameraDeltaMatrix(value));}
  function applyDraftPlacement(localRotation,anchorLocalRotation,manualAnchorRotation){return multiply(multiply(manualAnchorRotation,transpose(anchorLocalRotation)),localRotation);}
  function horizontalFovDeg(width,fx){const w=Number(width),f=Number(fx);return w>0&&f>0?2*Math.atan(w/(2*f))*180/Math.PI:null;}
  function unwrapDegreesNear(value,reference){const v=Number(value),r=Number(reference);return Number.isFinite(v)&&Number.isFinite(r)?v+360*Math.round((r-v)/360):v;}
  return {matrixToViewerEuler,viewerEulerToMatrix,localRotationToViewerMatrix,poseAtPts,applyDraftPlacement,rotateAboutWorldUp,localCameraDeltaMatrix,applyLocalCameraDelta,horizontalFovDeg,unwrapDegreesNear};
});
