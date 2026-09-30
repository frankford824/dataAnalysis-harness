import{ao as V,T as E,X as v,V as H,Q as N,R as b,ad as O,ap as T,F as j,Y as A,Z as w,I as k,aq as F,ar as y}from"./index-BkPo4X4E.js";let S=!1;function I(){if(V&&window.CSS&&!S&&(S=!0,"registerProperty"in window?.CSS))try{CSS.registerProperty({name:"--n-color-start",syntax:"<color>",inherits:!1,initialValue:"#0000"}),CSS.registerProperty({name:"--n-color-end",syntax:"<color>",inherits:!1,initialValue:"#0000"})}catch{}}function L(e){const{heightSmall:i,heightMedium:r,heightLarge:s,borderRadius:a}=e;return{color:"#eee",colorEnd:"#ddd",borderRadius:a,heightSmall:i,heightMedium:r,heightLarge:s}}const $={common:E,self:L},q=v([H("skeleton",`
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
 `)]),K=Object.assign(Object.assign({},w.props),{text:Boolean,round:Boolean,circle:Boolean,height:[String,Number],width:[String,Number],size:String,repeat:{type:Number,default:1},animated:{type:Boolean,default:!0},sharp:{type:Boolean,default:!0}}),Q=N({name:"Skeleton",inheritAttrs:!1,props:K,setup(e){I();const{mergedClsPrefixRef:i,mergedComponentPropsRef:r}=A(e),s=k(()=>{var n,o;return e.size||((o=(n=r?.value)===null||n===void 0?void 0:n.Skeleton)===null||o===void 0?void 0:o.size)}),a=w("Skeleton","-skeleton",q,$,e,i);return{mergedClsPrefix:i,style:k(()=>{var n,o;const m=a.value,{common:{cubicBezierEaseInOut:z}}=m,h=m.self,{color:_,colorEnd:x,borderRadius:C}=h;let l;const{circle:d,sharp:P,round:R,width:t,height:c,text:f,animated:B}=e,p=s.value;p!==void 0&&(l=h[F("height",p)]);const u=d?(n=t??c)!==null&&n!==void 0?n:l:t,g=(o=d?t??c:c)!==null&&o!==void 0?o:l;return{display:f?"inline-block":"",verticalAlign:f?"-0.125em":"",borderRadius:d?"50%":R?"4096px":P?"":C,width:typeof u=="number"?y(u):u,height:typeof g=="number"?y(g):g,animation:B?"":"none","--n-bezier":z,"--n-color-start":_,"--n-color-end":x}})}},render(){const{repeat:e,style:i,mergedClsPrefix:r,$attrs:s}=this,a=b("div",O({class:`${r}-skeleton`,style:i},s));return e>1?b(j,null,T(e,null).map(n=>[a,`
`])):a}});export{Q as _};
