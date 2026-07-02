// Test empty data and config when no parameters are defined (since you can add a Content-Type header to delete requests, an empty config alone would be allowed)
const axios = require('axios');
const id = 42
axios.delete(`https://petstore.swagger.io/v2/pets/${id}`, {}, {});