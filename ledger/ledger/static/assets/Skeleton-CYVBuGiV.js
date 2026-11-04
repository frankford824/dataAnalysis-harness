import{aM as N,d as O,o as v,m as E,e as H,h as b,aC as V,aN as j,K as A,g as K,w,k,aO as L,aP as y}from"./index-CN_o7DVJ.js";let S=!1;function M(){if(N&&window.CSS&&!S&&(S=!0,"registerProperty"in window?.CSS))try{CSS.registerProperty({name:"--n-color-start",syntax:"<color>",inherits:!1,initialValue:"#0000"}),CSS.registerProperty({name:"--n-color-end",syntax:"<color>",inherits:!1,initialValue:"#0000"})}catch{}}function T(e){const{heightSmall:i,heightMedium:r,heightLarge:s,borderRadius:a}=e;return{color:"#eee",colorEnd:"#ddd",borderRadius:a,heightSmall:i,heightMedium:r,heightLarge:s}}const $={common:O,self:T},F=v([E("skeleton",`
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
 `)]),I=Object.assign(Object.assign({},w.props),{text:Boolean,round:Boolean,circle:Boolean,height:[String,Number],width:[String,Number],size:String,repeat:{type:Number,default:1},animated:{type:Boolean,default:!0},sharp:{type:Boolean,default:!0}}),q=H({name:"Skeleton",inheritAttrs:!1,props:I,setup(e){M();const{mergedClsPrefixRef:i,mergedComponentPropsRef:r}=K(e),s=k(()=>{var n,o;return e.size||((o=(n=r?.value)===null||n===void 0?void 0:n.Skeleton)===null||o===void 0?void 0:o.size)}),a=w("Skeleton","-skeleton",F,$,e,i);return{mergedClsPrefix:i,style:k(()=>{var n,o;const m=a.value,{common:{cubicBezierEaseInOut:z}}=m,h=m.self,{color:C,colorEnd:P,borderRadius:_}=h;let l;const{circle:d,sharp:x,round:B,width:t,height:c,text:f,animated:R}=e,p=s.value;p!==void 0&&(l=h[L("height",p)]);const u=d?(n=t??c)!==null&&n!==void 0?n:l:t,g=(o=d?t??c:c)!==null&&o!==void 0?o:l;return{display:f?"inline-block":"",verticalAlign:f?"-0.125em":"",borderRadius:d?"50%":B?"4096px":x?"":_,width:typeof u=="number"?y(u):u,height:typeof g=="number"?y(g):g,animation:R?"":"none","--n-bezier":z,"--n-color-start":C,"--n-color-end":P}})}},render(){const{repeat:e,style:i,mergedClsPrefix:r,$attrs:s}=this,a=b("div",V({class:`${r}-skeleton`,style:i},s));return e>1?b(A,null,j(e,null).map(n=>[a,`
`])):a}});export{q as _};
