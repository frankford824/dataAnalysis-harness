import{X as r,V as i,W as n,aH as j,bo as W,bp as D,Y as F,T as I,Z as J,$ as h,aO as K,aP as N,J as s,ca as X,au as c}from"./index-CnCdZA5v.js";const Y=r([i("table",`
 font-size: var(--n-font-size);
 font-variant-numeric: tabular-nums;
 line-height: var(--n-line-height);
 width: 100%;
 border-radius: var(--n-border-radius) var(--n-border-radius) 0 0;
 text-align: left;
 border-collapse: separate;
 border-spacing: 0;
 overflow: hidden;
 background-color: var(--n-td-color);
 border-color: var(--n-merged-border-color);
 transition:
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier),
 color .3s var(--n-bezier);
 --n-merged-border-color: var(--n-border-color);
 `,[r("th",`
 white-space: nowrap;
 transition:
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier),
 color .3s var(--n-bezier);
 text-align: inherit;
 padding: var(--n-th-padding);
 vertical-align: inherit;
 text-transform: none;
 border: 0px solid var(--n-merged-border-color);
 font-weight: var(--n-th-font-weight);
 color: var(--n-th-text-color);
 background-color: var(--n-th-color);
 border-bottom: 1px solid var(--n-merged-border-color);
 border-right: 1px solid var(--n-merged-border-color);
 `,[r("&:last-child",`
 border-right: 0px solid var(--n-merged-border-color);
 `)]),r("td",`
 transition:
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier),
 color .3s var(--n-bezier);
 padding: var(--n-td-padding);
 color: var(--n-td-text-color);
 background-color: var(--n-td-color);
 border: 0px solid var(--n-merged-border-color);
 border-right: 1px solid var(--n-merged-border-color);
 border-bottom: 1px solid var(--n-merged-border-color);
 `,[r("&:last-child",`
 border-right: 0px solid var(--n-merged-border-color);
 `)]),n("bordered",`
 border: 1px solid var(--n-merged-border-color);
 border-radius: var(--n-border-radius);
 `,[r("tr",[r("&:last-child",[r("td",`
 border-bottom: 0 solid var(--n-merged-border-color);
 `)])])]),n("single-line",[r("th",`
 border-right: 0px solid var(--n-merged-border-color);
 `),r("td",`
 border-right: 0px solid var(--n-merged-border-color);
 `)]),n("single-column",[r("tr",[r("&:not(:last-child)",[r("td",`
 border-bottom: 0px solid var(--n-merged-border-color);
 `)])])]),n("striped",[r("tr:nth-of-type(even)",[r("td","background-color: var(--n-td-color-striped)")])]),j("bottom-bordered",[r("tr",[r("&:last-child",[r("td",`
 border-bottom: 0px solid var(--n-merged-border-color);
 `)])])])]),W(i("table",`
 background-color: var(--n-td-color-modal);
 --n-merged-border-color: var(--n-border-color-modal);
 `,[r("th",`
 background-color: var(--n-th-color-modal);
 `),r("td",`
 background-color: var(--n-td-color-modal);
 `)])),D(i("table",`
 background-color: var(--n-td-color-popover);
 --n-merged-border-color: var(--n-border-color-popover);
 `,[r("th",`
 background-color: var(--n-th-color-popover);
 `),r("td",`
 background-color: var(--n-td-color-popover);
 `)]))]),Z=Object.assign(Object.assign({},h.props),{bordered:{type:Boolean,default:!0},bottomBordered:{type:Boolean,default:!0},singleLine:{type:Boolean,default:!0},striped:Boolean,singleColumn:Boolean,size:String}),A=F({name:"Table",props:Z,setup(e){const{mergedClsPrefixRef:o,inlineThemeDisabled:b,mergedRtlRef:p,mergedComponentPropsRef:a}=J(e),v=s(()=>{var d,l;return e.size||((l=(d=a?.value)===null||d===void 0?void 0:d.Table)===null||l===void 0?void 0:l.size)||"medium"}),m=h("Table","-table",Y,X,e,o),u=K("Table",p,o),g=s(()=>{const d=v.value,{self:{borderColor:l,tdColor:f,tdColorModal:C,tdColorPopover:x,thColor:z,thColorModal:P,thColorPopover:k,thTextColor:R,tdTextColor:T,borderRadius:B,thFontWeight:$,lineHeight:_,borderColorModal:w,borderColorPopover:y,tdColorStriped:M,tdColorStripedModal:S,tdColorStripedPopover:E,[c("fontSize",d)]:O,[c("tdPadding",d)]:V,[c("thPadding",d)]:H},common:{cubicBezierEaseInOut:L}}=m.value;return{"--n-bezier":L,"--n-td-color":f,"--n-td-color-modal":C,"--n-td-color-popover":x,"--n-td-text-color":T,"--n-border-color":l,"--n-border-color-modal":w,"--n-border-color-popover":y,"--n-border-radius":B,"--n-font-size":O,"--n-th-color":z,"--n-th-color-modal":P,"--n-th-color-popover":k,"--n-th-font-weight":$,"--n-th-text-color":R,"--n-line-height":_,"--n-td-padding":V,"--n-th-padding":H,"--n-td-color-striped":M,"--n-td-color-striped-modal":S,"--n-td-color-striped-popover":E}}),t=b?N("table",s(()=>v.value[0]),g,e):void 0;return{rtlEnabled:u,mergedClsPrefix:o,cssVars:b?void 0:g,themeClass:t?.themeClass,onRender:t?.onRender}},render(){var e;const{mergedClsPrefix:o}=this;return(e=this.onRender)===null||e===void 0||e.call(this),I("table",{class:[`${o}-table`,this.themeClass,{[`${o}-table--rtl`]:this.rtlEnabled,[`${o}-table--bottom-bordered`]:this.bottomBordered,[`${o}-table--bordered`]:this.bordered,[`${o}-table--single-line`]:this.singleLine,[`${o}-table--single-column`]:this.singleColumn,[`${o}-table--striped`]:this.striped}],style:this.cssVars},this.$slots)}});export{A as _};
