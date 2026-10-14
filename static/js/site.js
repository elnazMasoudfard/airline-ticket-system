/*
 * Small site-wide behaviour (no dependencies):
 *  1. mobile navigation toggle
 *  2. protection against double submits of forms
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------- 1. mobile nav
  var header = document.querySelector('.site-header');
  var toggle = document.querySelector('.nav-toggle');

  function setNavOpen(open) {
    if (!header || !toggle) { return; }
    header.classList.toggle('nav-open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  if (header && toggle) {
    toggle.addEventListener('click', function () {
      setNavOpen(!header.classList.contains('nav-open'));
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && header.classList.contains('nav-open')) {
        setNavOpen(false);
        toggle.focus();
      }
    });

    // Close the menu after choosing a link.
    header.querySelectorAll('.main-nav a').forEach(function (link) {
      link.addEventListener('click', function () { setNavOpen(false); });
    });
  }

  // ---------------------------------------------------- 2. no double form submits
  // A second click on "pay" or "cancel" while the first request is still running must not
  // send the form again. (The server is safe against it too; this only avoids the confusing
  // second message.) Opt out with data-no-lock on a form; the ajax search form is skipped.
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) { return; }
    if (event.defaultPrevented) { return; }
    if (form.hasAttribute('data-no-lock') || form.classList.contains('search-form')) { return; }

    // Disable the buttons only AFTER the browser has collected the form data: a disabled
    // submit button would otherwise be left out of the request (e.g. name="send_code").
    setTimeout(function () {
      if (event.defaultPrevented) { return; }  // another handler cancelled the submit
      form.querySelectorAll('button[type="submit"], button:not([type])').forEach(function (button) {
        button.disabled = true;
        button.setAttribute('data-locked', '1');
      });
    }, 0);
  });

  // Coming back with the browser's Back button must not show dead buttons.
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('[data-locked]').forEach(function (button) {
      button.disabled = false;
      button.removeAttribute('data-locked');
    });
  });
})();