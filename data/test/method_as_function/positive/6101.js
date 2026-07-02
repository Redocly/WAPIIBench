// Test concatenating variable with string
const axios = require('axios');
const id = 42
const scheme = 'Bearer ';
axios.get(`https://petstore.swagger.io/v2/pets-secure/${id}`, {
    headers: {Authorization: scheme + 'mySuperSecureToken'}
});