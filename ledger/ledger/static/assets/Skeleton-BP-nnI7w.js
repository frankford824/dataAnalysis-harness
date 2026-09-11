import{ac as j,ad as E,ae as v,af as H,Q as N,R as b,a3 as O,ag as V,F as A,ah as F,ai as w,I as k,aj as I,ak as y}from"./index-BRz8wdHj.js";let S=!1;function L(){if(j&&window.CSS&&!S&&(S=!0,"registerProperty"in window?.CSS))try{CSS.registerProperty({name:"--n-color-start",syntax:"<color>",inherits:!1,initialValue:"#0000"}),CSS.registerProperty({name:"--n-color-end",syntax:"<color>",inherits:!1,initialValue:"#0000"})}catch{}}function T(e){const{heightSmall:i,heightMedium:r,heightLarge:s,borderRadius:a}=e;return{color:"#eee",colorEnd:"#ddd",borderRadius:a,heightSmall:i,heightMedium:r,heightLarge:s}}const $={common:E,self:T},K=v([H("skeleton",`
 height: 1em;
 width: 100%;
 transition:
 --n-color-start .3s var(--n-bezier),
 --n-color-end .3s var(--n-bezier),
 background-color .3s var(--n-bezier);
 animation: 2s skeleton-loading infinite cubic-bezier(0.36, 0, 0.64, 1);
 background-color: var(--n-color-start);
 `),v("@keyframes skeleton-loading",`
 0% {
 background: var(--n-color-start);
 }
 40% {
 background: var(--n-color-end);
 }
 80% {
 background: var(--n-color-start);
 }
 100% {
 background: var(--n-color-start);
 }
 `)]),M=Object.assign(Object.assign({},w.props),{text:Boolean,round:Boolean,circle:Boolean,height:[String,Number],width:[String,Number],size:String,repeat:{type:Number,default:1},animated:{type:Boolean,default:!0},sharp:{type:Boolean,default:!0}}),W=N({name:"Skeleton",inheritAttrs:!1,props:M,setup(e){L();const{mergedClsPrefixRef:i,mergedComponentPropsRef:r}=F(e),s=k(()=>{var n,o;return e.size||((o=(n=r?.value)===null||n===void 0?void 0:n.Skeleton)===null||o===void 0?void 0:o.size)}),a=w("Skeleton","-skeleton",K,$,e,i);return{mergedClsPrefix:i,style:k(()=>{var n,o;const m=a.value,{common:{cubicBezierEaseInOut:z}}=m,h=m.self,{color:_,colorEnd:x,borderRadius:C}=h;let l;const{circle:d,sharp:P,round:R,width:t,height:c,text:f,animated:B}=e,p=s.value;p!==void 0&&(l=h[I("height",p)]);const u=d?(n=t??c)!==null&&n!==void 0?n:l:t,g=(o=d?t??c:c)!==null&&o!==void 0?o:l;return{display:f?"inline-block":"",verticalAlign:f?"-0.125em":"",borderRadius:d?"50%":R?"4096px":P?"":C,width:typeof u=="number"?y(u):u,height:typeof g=="number"?y(g):g,animation:B?"":"none","--n-bezier":z,"--n-color-start":_,"--n-color-end":x}})}},render(){const{repeat:e,style:i,mergedClsPrefix:r,$attrs:s}=this,a=b("div",O({class:`${r}-skeleton`,style:i},s));return e>1?b(A,null,V(e,null).map(n=>[a,`
`])):a}});export{W as _};
