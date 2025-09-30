        <script>
            // Coordinates data passed from Flask
            var coordinates = {{ coordinates|tojson }};
            
            // Initialize the map
            var map = L.map('map').setView([0, 0], 2);
            // Define custom icon
            var customIcon = L.icon({
                iconUrl: "{{ url_for('static', filename='images/icons/leaflet_town.png') }}",
                iconSize: [25, 33],    // Size of the icon
                iconAnchor: [12, 33],  // Point of the icon which will correspond to marker's location
                popupAnchor: [-3, -76] // Point from which the popup should open relative to the iconAnchor
            });
            
            // Add a tile layer to the map
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
            }).addTo(map);

            // Add all points to the map
            var markers = [];
            coordinates.forEach(function(coord) {
                var marker = L.marker([coord.lat, coord.lng], { icon: customIcon }).addTo(map);
                markers.push(marker);
            });

            // Adjust the map view to fit all markers
            if (markers.length > 0) {
                var group = new L.featureGroup(markers);
                map.fitBounds(group.getBounds());
            }
        </script>
    <script> 
            var thisurl = document.URL; 
            document.getElementById("url").innerHTML = thisurl; 
    </script> 
    <script>
        function includeHTML() {
            var z, i, elmnt, file, xhttp;
            /* loop through a collection of all HTML elements: */
            z = document.getElementsByTagName("*");
            for (i = 0; i < z.length; i++) {
                elmnt = z[i];
                /* search for elements with a certain attribute: */
                file = elmnt.getAttribute("w3-include-html");
                if (file) {
                    /* make an HTTP request using the attribute value as the file name: */
                    xhttp = new XMLHttpRequest();
                    xhttp.onreadystatechange = function() {
                        if (this.readyState == 4) {
                            if (this.status == 200) { elmnt.innerHTML = this.responseText; }
                            if (this.status == 404) { elmnt.innerHTML = "Page not found."; }
                            /* remove the attribute, and call this function once more: */
                            elmnt.removeAttribute("w3-include-html");
                            includeHTML();
                        }
                    }
                    xhttp.open("GET", file, true);
                    xhttp.send();
                    /* exit the function: */
                    return;
                }
            }
        }

        document.addEventListener("DOMContentLoaded", function() {
            includeHTML();
            document.getElementById("defaultOpen").click();
        });

        function openTab(evt, tabName) {
            var i, tabcontent, tablinks;
            tabcontent = document.getElementsByClassName("tabcontent");
            for (i = 0; i < tabcontent.length; i++) {
                tabcontent[i].style.display = "none";
            }
            tablinks = document.getElementsByClassName("tablinks");
            for (i = 0; i < tablinks.length; i++) {
                tablinks[i].className = tablinks[i].className.replace(" active", "");
            }
            document.getElementById(tabName).style.display = "block";
            evt.currentTarget.className += " active";
        }
    </script>
	<script>
        // Function to format the current date
        function formatDate(date) {
            const day = date.getDate();
            const month = date.toLocaleString('en-GB', { month: 'long' });
            const year = date.getFullYear();
            return `${day} ${month} ${year}`;
        }

        // Function to set the URL and date
        function setDynamicContent() {
            // Get the current date
            const currentDate = new Date();

            // Get the current URL
            const currentURL = window.location.href;

            // Set the date and URL in the respective spans
            document.getElementById('datetime').innerText = formatDate(currentDate);
            document.getElementById('url').innerText = currentURL;
        }

        // Run the function on page load
        window.onload = setDynamicContent;
    </script>