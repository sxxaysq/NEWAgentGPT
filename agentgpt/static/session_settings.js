/* A loaded conversation must not change the defaults used for a new goal. */
(function (root) {
  const clone = value => JSON.parse(JSON.stringify(value));
  function create(initial) {
    let template = clone(initial);
    return {
      load: settings => clone(settings),
      newSession: () => clone(template),
      save(settings, selected) {
        if (!selected) template = clone(settings);
        return clone(settings);
      },
    };
  }
  const api = {create};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.AgentSessionSettings = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
