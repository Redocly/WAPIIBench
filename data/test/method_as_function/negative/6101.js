// Test concatenating variable with variable (which is not permitted)
const axios = require('axios');
const id = 42
const scheme = 'Bearer ';
const token = 'mySuperSecureToken';
axios.get(`https://petstore.swagger.io/v2/pets-secure/${id}`, {
    headers: {Authorization: scheme + token}
});