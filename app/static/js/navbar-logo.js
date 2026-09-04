//
// app/static/js/navbar-logo.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * @module navbar-logo
 *
 * The CSS logo easter egg is a :hover rule, so touch devices never got it.
 * This gives them the same pulse on tap - answered instead of the "/"
 * navigation, which would kill the animation (the back button covers
 * sub-pages, "/" is just a reload on the dashboard). Hover devices are
 * untouched.
 */

const LOGO_TAP_CLASS = "logo-easter-tap";

if (window.matchMedia("(hover: none)").matches) {
    const logo = document.querySelector(".top-navbar .navbar-brand .logo");
    const brand = logo?.closest("a.navbar-brand");

    if (logo instanceof HTMLElement && brand instanceof HTMLAnchorElement) {
        brand.addEventListener("click", (event) => {
            event.preventDefault();
            // A retap while the pulse still runs restarts it rather than being
            // swallowed by the class already being present.
            logo.classList.remove(LOGO_TAP_CLASS);
            void logo.offsetWidth;
            logo.classList.add(LOGO_TAP_CLASS);
        });

        logo.addEventListener("animationend", () => {
            logo.classList.remove(LOGO_TAP_CLASS);
        });
    }
}