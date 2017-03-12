(function (i, s, o, g, r, a, m) {
    i['GoogleAnalyticsObject'] = r;
    i[r] = i[r] || function () {
            (i[r].q = i[r].q || []).push(arguments)
        }, i[r].l = 1 * new Date();
    a = s.createElement(o),
        m = s.getElementsByTagName(o)[0];
    a.async = 1;
    a.src = g;
    m.parentNode.insertBefore(a, m)
})(window, document, 'script', 'https://www.google-analytics.com/analytics.js', 'ga');



  ga('create', 'UA-92189178-1', 'auto');
  ga('send', 'pageview');

map.on('locationfound', onLocationFound);
var current_position, current_accuracy;
function onLocationFound(e) {
    if (current_position) {
        map.removeLayer(current_position);
        map.removeLayer(current_accuracy);
    }
    var radius = e.accuracy / 2;
    current_position = L.marker(e.latlng).addTo(map);
    current_accuracy = L.circle(e.latlng, radius).addTo(map);
}
