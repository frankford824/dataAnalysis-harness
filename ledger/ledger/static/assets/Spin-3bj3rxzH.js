import{d as S,o as h,m as p,ci as C,au as f,e as x,h as r,bb as T,bj as k,g as w,w as v,j as $,bx as j,k as m,x as R,cj as _,aU as O,aT as B,ca as P}from"./index-Co0Jlp1N.js";function L(e){const{opacityDisabled:a,heightTiny:t,heightSmall:i,heightMedium:l,heightLarge:n,heightHuge:c,primaryColor:o,fontSize:s}=e;return{fontSize:s,textColor:o,sizeTiny:t,sizeSmall:i,sizeMedium:l,sizeLarge:n,sizeHuge:c,color:o,opacitySpinning:a}}const V={common:S,self:L},H=h([h("@keyframes spin-rotate",`
 from {
 transform: rotate(0);
 }
 to {
 transform: rotate(360deg);
 }
 `),p("spin-container",`
 position: relative;
 `,[p("spin-body",`
 position: absolute;
 top: 50%;
 left: 50%;
 transform: translateX(-50%) translateY(-50%);
 `,[C()])]),p("spin-body",`
 display: inline-flex;
 align-items: center;
 justify-content: center;
 flex-direction: column;
 `),p("spin",`
 display: inline-flex;
 height: var(--n-size);
 width: var(--n-size);
 font-size: var(--n-size);
 color: var(--n-color);
 `,[f("rotate",`
 animation: spin-rotate 2s linear infinite;
 `)]),p("spin-description",`
 display: inline-block;
 font-size: var(--n-font-size);
 color: var(--n-text-color);
 transition: color .3s var(--n-bezier);
 margin-top: 8px;
 `),p("spin-content",`
 opacity: 1;
 transition: opacity .3s var(--n-bezier);
 pointer-events: all;
 `,[f("spinning",`
 user-select: none;
 -webkit-user-select: none;
 pointer-events: none;
 opacity: var(--n-opacity-spinning);
 `)])]),I={small:20,medium:18,large:16},N=Object.assign(Object.assign(Object.assign({},v.props),{contentClass:String,contentStyle:[Object,String],description:String,size:{type:[String,Number],default:"medium"},show:{type:Boolean,default:!0},rotate:{type:Boolean,default:!0},spinning:{type:Boolean,validator:()=>!0,default:void 0},delay:Number}),_),D=x({name:"Spin",props:N,slots:Object,setup(e){const{mergedClsPrefixRef:a,inlineThemeDisabled:t}=w(e),i=v("Spin","-spin",H,V,e,a),l=m(()=>{const{size:s}=e,{common:{cubicBezierEaseInOut:d},self:u}=i.value,{opacitySpinning:g,color:y,textColor:b}=u,z=typeof s=="number"?O(s):u[B("size",s)];return{"--n-bezier":d,"--n-opacity-spinning":g,"--n-size":z,"--n-color":y,"--n-text-color":b}}),n=t?$("spin",m(()=>{const{size:s}=e;return typeof s=="number"?String(s):s[0]}),l,e):void 0,c=P(e,["spinning","show"]),o=R(!1);return j(s=>{let d;if(c.value){const{delay:u}=e;if(u){d=window.setTimeout(()=>{o.value=!0},u),s(()=>{clearTimeout(d)});return}}o.value=c.value}),{mergedClsPrefix:a,active:o,mergedStrokeWidth:m(()=>{const{strokeWidth:s}=e;if(s!==void 0)return s;const{size:d}=e;return I[typeof d=="number"?"medium":d]}),cssVars:t?void 0:l,themeClass:n?.themeClass,onRender:n?.onRender}},render(){var e,a;const{$slots:t,mergedClsPrefix:i,description:l}=this,n=t.icon&&this.rotate,c=(l||t.description)&&r("div",{class:`${i}-spin-description`},l||((e=t.description)===null||e===void 0?void 0:e.call(t))),o=t.icon?r("div",{class:[`${i}-spin-body`,this.themeClass]},r("div",{class:[`${i}-spin`,n&&`${i}-spin--rotate`],style:t.default?"":this.cssVars},t.icon()),c):r("div",{class:[`${i}-spin-body`,this.themeClass]},r(T,{clsPrefix:i,style:t.default?"":this.cssVars,stroke:this.stroke,"stroke-width":this.mergedStrokeWidth,radius:this.radius,scale:this.scale,class:`${i}-spin`}),c);return(a=this.onRender)===null||a===void 0||a.call(this),t.default?r("div",{class:[`${i}-spin-container`,this.themeClass],style:this.cssVars},r("div",{class:[`${i}-spin-content`,this.active&&`${i}-spin-content--spinning`,this.contentClass],style:this.contentStyle},t),r(k,{name:"fade-in-transition"},{default:()=>this.active?o:null})):o}});export{D as _};
