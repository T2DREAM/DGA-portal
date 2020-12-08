import React from 'react';
import PropTypes from 'prop-types';


/* eslint-disable jsx-a11y/anchor-is-valid */
const Footer = ({ version }, reactContext) => {
    const session = reactContext.session;
    const disabled = !session;
    let userActionRender;

    if (!(session && session['auth.userid'])) {
        userActionRender = <a href="#" data-trigger="login" disabled={disabled}>User sign-in</a>;
    } else {
        userActionRender = <a href="#" data-trigger="logout">User sign out</a>;
    }
    return (
        <footer id="page-footer">
            <div className="container">
                <div className="row">
                    <div className="app-version">{version}</div>
                </div>
            </div>
            <div className="page-footer">
                <div className="container">
                    <div className="row">
                        <div className="footer-sections">
                            <div className="footer-links-section">
                                <ul className="footer-links">
                                    <li><a href="/help/citing-dga">Citing DGA</a></li>
                                    <li><a href="https://ucsd.edu/about/terms-of-use.html">Privacy</a></li>
                                    <li><a href="mailto:t2dream-l@mailman.ucsd.edu">Contact</a></li>
                                </ul>
                                <ul className="footer-links">
                                    <li id="user-actions-footer">{userActionRender}</li>
                                </ul>
                            </div>

                            <div className="footer-logos-section">
                                <ul className="footer-logos">
                                <li><a href="/"><img src="/static/img/logo_final.png" alt="Diabetes Epigenome Atlas" id="t2dream-logo" height="75px" width="75px" /></a></li>
                                <li><a href="http://www.ucsd.edu"><img src="/static/img/UCSanDiegoLogo-BlueGold.png" alt="UC San Diego" id="ucsd-logo" width="80px" height="42px" /></a></li>
        </ul>
                            </div>
                        </div>
                        <p className="copy-notice">&copy;{new Date().getFullYear()} Regents of the University of California</p>
                    </div>
                </div>
            </div>
        </footer>
    );
};
/* eslint-enable jsx-a11y/anchor-is-valid */

Footer.contextTypes = {
    session: PropTypes.object,
};

Footer.propTypes = {
    version: PropTypes.string, // App version number
};

Footer.defaultProps = {
    version: '',
};

export default Footer;
