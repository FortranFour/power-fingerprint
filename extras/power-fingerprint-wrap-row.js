/* Power Fingerprint readable rows v1.0.0.
 * Uses the installed template-entity-row for all templates and actions.
 * Only text layout changes; one stylesheet is shared by all row instances.
 */
const ROW_TAG = 'power-fingerprint-wrap-row';
const BASE_TAG = 'template-entity-row';
const WRAP_CSS = `
  :host { display: block; min-width: 0; }
  #wrapper { width: 100%; box-sizing: border-box; height: auto; align-items: flex-start; }
  #wrapper .flex { display: flex; flex: 1 1 0%; min-width: 0; }
  .info, .info > *, .secondary, .state {
    min-width: 0;
    max-height: none !important;
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: clip !important;
    overflow-wrap: anywhere;
    word-break: normal;
    -webkit-line-clamp: unset !important;
  }
  .info { flex: 1 1 0%; }
  .info > .secondary { white-space: pre-line !important; }
  .state { flex: 0 1 auto; max-width: 40%; }
  :host([pf-wrap-head]) {
    container-type: inline-size;
    container-name: pf-readable-row;
  }
  @container pf-readable-row (max-width: 440px) {
    #wrapper {
      display: grid !important;
      grid-template-columns: 40px minmax(0, 1fr);
      grid-template-areas: "badge info" ". state";
      column-gap: 16px;
      row-gap: 2px;
      align-items: center;
    }
    #wrapper > state-badge { grid-area: badge; }
    #wrapper > .info {
      grid-area: info;
      padding: 0 !important;
      margin: 0 !important;
    }
    #wrapper > .state {
      grid-area: state;
      max-width: 100%;
      margin: 0 !important;
      justify-self: start;
      text-align: start;
    }
    #wrapper > .flex {
      grid-area: info;
      flex-direction: column;
      align-items: stretch;
    }
    #wrapper > .flex .info { padding: 0 !important; margin: 0 !important; }
    #wrapper > .flex .state {
      max-width: 100%;
      margin: 2px 0 0 !important;
      text-align: start;
    }
  }
  #wrapper.hidden { display: none !important; }
`;

await customElements.whenDefined(BASE_TAG);
if (!customElements.get(ROW_TAG)) {
  const BaseRow = customElements.get(BASE_TAG);
  const canShareSheet = typeof CSSStyleSheet === 'function'
    && 'adoptedStyleSheets' in Document.prototype
    && 'replaceSync' in CSSStyleSheet.prototype
    && (!globalThis.ShadyCSS || globalThis.ShadyCSS.nativeShadow);
  const sheet = canShareSheet ? new CSSStyleSheet() : null;
  if (sheet) sheet.replaceSync(WRAP_CSS);

  class PowerFingerprintWrapRow extends BaseRow {
    static get styles() {
      return sheet ? [super.styles, sheet] : super.styles;
    }
    createRenderRoot() {
      const root = super.createRenderRoot();
      if (!sheet) {
        const style = document.createElement('style');
        style.textContent = WRAP_CSS;
        root.prepend(style);
      }
      return root;
    }
    setConfig(config) {
      this.toggleAttribute('pf-wrap-head', config.pf_wrap_head === true);
      super.setConfig(config);
    }
  }
  customElements.define(ROW_TAG, PowerFingerprintWrapRow);
}
