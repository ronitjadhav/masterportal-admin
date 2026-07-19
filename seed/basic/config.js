const Config = {
    alerting: {
        fetchBroadcastUrl: "./resources/newsFeedPortalAlerts.json"
    },
    namedProjections: [
        ["EPSG:25832", "+title=ETRS89/UTM 32N +proj=utm +zone=32 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs"]
    ],
    layerConf: "./resources/services.json",
    restConf: "./resources/rest-services.json",
    styleConf: "./resources/style_v3.json",
    wfsImgPath: "./resources/img/",
    portalLanguage: {
        enabled: true,
        debug: false,
        languages: {
            de: "Deutsch",
            en: "English",
            es: "Español",
            it: "Italiano",
            platt: "Platt",
            pt: "Português",
            ru: "Русский",
            tr: "Türkçe",
            ua: "Українська",
            nl: "Nederlands"
        },
        fallbackLanguage: "de",
        changeLanguageOnStartWhen: ["querystring", "localStorage", "htmlTag"]
    },
    portalLocales: {
        de: {
            common: {
                modules: {
                    searchBar: {
                        placeholder: {
                            address: "Suche nach Themen"
                        }
                    }
                }
            }
        }
    }
};

if (typeof module !== "undefined") {
    module.exports = Config;
}
